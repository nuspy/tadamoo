package app.basalt.basalt_mobile

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import ai.onnxruntime.TensorInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.nio.LongBuffer
import java.security.MessageDigest
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * The package a personalised engine (Ultralight with wenet, FeatherTalk) gives the phone, opened only after its files were checked: the folder is
 * inside the app's own, `phone.json` has the SHA-256 the avatar's manifest states, and every file it lists has its size and SHA-256. Written by
 * voice/avatar_engines/phone_export.py; the format is described there.
 */
class PersonalPack private constructor(val dir: File, val json: JSONObject) {
    val engine: String = json.getString("engine")
    val fps: Int = json.getInt("fps")
    val size: Int = json.getInt("size")
    val front: JSONObject = json.getJSONObject("frontend")
    val modelFile: String = json.getJSONObject("model").getString("file")
    val audioShape: LongArray = longs(json.getJSONObject("model").getJSONArray("audio"))

    /** A file of the package; refuses a name that leaves the folder. */
    fun file(rel: String): File {
        val f = File(dir, rel).canonicalFile
        require(!rel.contains("..") && !rel.startsWith("/") && !rel.contains("\\") && f.path.startsWith(dir.canonicalPath + File.separator)) { "path $rel" }
        return f
    }

    companion object {
        fun longs(a: JSONArray) = LongArray(a.length()) { a.getLong(it) }

        fun sha256(f: File): String {
            val md = MessageDigest.getInstance("SHA-256")
            f.inputStream().use { input ->
                val buf = ByteArray(1 shl 16)
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    md.update(buf, 0, n)
                }
            }
            return md.digest().joinToString("") { "%02x".format(it) }
        }

        /** Opens the package in `dir` (inside `appRoot`); `phoneSha` is the SHA-256 of `phone.json` the avatar's manifest states. */
        fun open(appRoot: File, dir: File, phoneSha: String): Pair<PersonalPack?, LoadResult> {
            try {
                val base = dir.canonicalFile
                if (!base.path.startsWith(appRoot.canonicalPath + File.separator)) return Pair(null, LoadResult("outside_app_dir", dir.path))
                val phone = File(base, "phone.json")
                if (!phone.isFile) return Pair(null, LoadResult("no_pack", "phone.json"))
                if (!sha256(phone).equals(phoneSha, ignoreCase = true)) return Pair(null, LoadResult("hash_mismatch", "phone.json"))
                val json = JSONObject(phone.readText())
                if (json.getInt("version") != 1) return Pair(null, LoadResult("bad_pack", "version"))
                val pack = PersonalPack(base, json)
                val files = json.getJSONObject("files")
                for (rel in files.keys()) {
                    val meta = files.getJSONObject(rel)
                    val f = pack.file(rel)
                    if (!f.isFile || f.length() != meta.getLong("bytes")) return Pair(null, LoadResult("hash_mismatch", rel))
                    if (!sha256(f).equals(meta.getString("sha256"), ignoreCase = true)) return Pair(null, LoadResult("hash_mismatch", rel))
                }
                return Pair(pack, LoadResult())
            } catch (e: Exception) {
                return Pair(null, LoadResult("bad_pack", e.message ?: e.javaClass.simpleName))
            }
        }
    }
}

/** The voice of one reply at its own rate, as 16 kHz samples in [-1, 1] (linear interpolation, as the Wav2Lip path does). */
class Resampler(private val rate: Int) {
    private val ratio = rate / 16000.0
    private var src = FloatArray(rate * 2)
    private var srcLen = 0
    private var outIdx = 0L

    fun feed(pcm: ByteArray): FloatArray {
        val n = pcm.size / 2
        if (srcLen + n + 1 > src.size) src = src.copyOf(max(src.size * 2, srcLen + n + 1))
        for (i in 0 until n) {
            val s = ((pcm[2 * i + 1].toInt() shl 8) or (pcm[2 * i].toInt() and 0xFF)).toShort()
            src[srcLen++] = s / 32768f
        }
        return pull()
    }

    /** No more voice: what is left is taken against one sample of silence. */
    fun finish(): FloatArray {
        if (srcLen + 1 > src.size) src = src.copyOf(src.size + 1)
        src[srcLen++] = 0f
        return pull()
    }

    private fun pull(): FloatArray {
        val out = FloatArray(max(1, ((srcLen - outIdx * ratio) / ratio).toInt() + 2))
        var k = 0
        while (outIdx * ratio + 1 < srcLen) {
            val pos = outIdx * ratio
            val i = pos.toInt()
            val frac = (pos - i).toFloat()
            out[k++] = src[i] * (1f - frac) + src[i + 1] * frac
            outIdx++
        }
        return out.copyOf(k)
    }
}

/** What turns the 16 kHz voice into the audio input of the visual model of each video frame. */
interface Frontend {
    fun add(x: FloatArray, n: Int)
    fun finish()

    /** The voice is over and frame `i` is past the last one the audio has. */
    fun ended(i: Int): Boolean

    /** The audio input of the model for frame `i`, or null when the voice has not got far enough. */
    fun feature(i: Int): FloatArray?
}

private fun floatsOf(f: File): FloatArray {
    val b = ByteBuffer.wrap(f.readBytes()).order(ByteOrder.LITTLE_ENDIAN)
    return FloatArray(b.remaining() / 4).also { b.asFloatBuffer().get(it) }
}

private fun tensorOf(env: OrtEnvironment, data: FloatArray, shape: LongArray) = OnnxTensor.createTensor(env, FloatBuffer.wrap(data), shape)

/**
 * FeatherHuBERT, an ONNX model that takes the waveform: 320 samples make one 1024 feature, two features one video frame, and the visual model sees 20 frames
 * around the one it draws. The waveform is normalised to zero mean and unit variance (the whole reply on the PC; here what has arrived), and encoded in blocks
 * of `block_tokens` features with `context_tokens` before them for the model to see; only the new features are kept.
 */
class FeatherFrontend(private val env: OrtEnvironment, private val enc: OrtSession, f: JSONObject) : Frontend {
    private val tokenSamples = f.getInt("token_samples")
    private val kernel = f.getInt("kernel")
    private val dim = f.getInt("dim")
    private val perFrame = f.getInt("tokens_per_frame")
    private val windowFrames = f.getInt("window_frames")
    private val block = f.getInt("block_tokens")
    private val context = f.getInt("context_tokens")
    private val inName = f.getString("input")
    private val overlap = kernel - tokenSamples
    private var wave = FloatArray(16000 * 4)
    private var n = 0
    private var sum = 0.0
    private var sumSq = 0.0
    private val tokens = ArrayList<FloatArray>()
    private var finished = false

    private fun available() = if (n < kernel) 0 else (n - overlap) / tokenSamples

    override fun add(x: FloatArray, n: Int) {
        if (this.n + n > wave.size) wave = wave.copyOf(max(wave.size * 2, this.n + n))
        for (i in 0 until n) {
            val v = x[i]
            wave[this.n++] = v
            sum += v
            sumSq += v.toDouble() * v
        }
        encode()
    }

    override fun finish() {
        finished = true
        encode()
    }

    private fun encode() {
        while (true) {
            val have = available()
            val upTo = if (finished) have else tokens.size + block
            if (upTo <= tokens.size || upTo > have) return
            encodeBlock(tokens.size, upTo)
        }
    }

    private fun encodeBlock(done: Int, upTo: Int) {
        val first = max(0, done - context)
        val from = first * tokenSamples
        val len = upTo * tokenSamples + overlap - from
        val mean = (sum / n).toFloat()
        val inv = (1.0 / sqrt(max(0.0, sumSq / n - (sum / n) * (sum / n)) + 1e-7)).toFloat()
        val x = FloatArray(len) { (wave[from + it] - mean) * inv }
        val t = tensorOf(env, x, longArrayOf(1, len.toLong()))
        try {
            enc.run(mapOf(inName to t)).use { r ->
                val out = r[0] as OnnxTensor
                val count = out.info.shape[1].toInt()
                if (count < upTo - first || out.info.shape[2].toInt() != dim) throw IllegalStateException("bad_io")
                val buf = out.floatBuffer
                for (k in (done - first) until (upTo - first)) {
                    val tok = FloatArray(dim)
                    buf.position(k * dim)
                    buf.get(tok)
                    tokens.add(tok)
                }
            }
        } finally {
            t.close()
        }
    }

    override fun ended(i: Int) = finished && i >= tokens.size / perFrame

    override fun feature(i: Int): FloatArray? {
        val half = windowFrames / 2
        if (!finished && perFrame * (i + half) > tokens.size) return null
        val frames = tokens.size / perFrame
        val out = FloatArray(windowFrames * perFrame * dim)
        for (w in 0 until windowFrames) {
            val j = i - half + w
            if (j < 0 || j >= frames) continue
            for (p in 0 until perFrame) System.arraycopy(tokens[j * perFrame + p], 0, out, (w * perFrame + p) * dim, dim)
        }
        return out
    }
}

/**
 * Wenet's front end for Ultralight: the waveform at 16-bit scale between 5120 zeros before and 5600 after, Kaldi's filter bank (an ONNX graph for 67 frames) and
 * the wenet encoder on those 67 frames, one chunk per video frame, 800 samples further each (no state is carried: its caches stay zero, as in wenet_infer.py).
 * The visual model sees 8 chunks around the frame it draws.
 */
class WenetFrontend(private val env: OrtEnvironment, private val fbank: OrtSession, private val enc: OrtSession, f: JSONObject) : Frontend {
    private val front = f.getInt("pad_front")
    private val back = f.getInt("pad_back")
    private val step = f.getInt("step_samples")
    private val chunkSamples = f.getInt("fbank_samples")
    private val chunkFrames = f.getInt("chunk_frames")
    private val mels = f.getInt("mels")
    private val shift = f.getInt("shift")
    private val frameLen = f.getInt("frame_len")
    private val frameFloats = f.getInt("frame_floats")
    private val window = f.getInt("window_frames")
    private val scale = f.getInt("pcm_scale").toFloat()
    private val stepFrames = step / shift
    private val state = f.getJSONObject("state")
    private var wave = FloatArray(16000 * 4)
    private var n = front
    private val encoded = ArrayList<FloatArray>()
    private var finished = false

    override fun add(x: FloatArray, n: Int) {
        if (this.n + n > wave.size) wave = wave.copyOf(max(wave.size * 2, this.n + n))
        for (i in 0 until n) wave[this.n++] = (x[i] * scale).coerceIn(-32768f, 32767f).toInt().toFloat()
        while (this.n >= step * encoded.size + chunkSamples) chunk(encoded.size)
    }

    override fun finish() {
        if (finished) return
        if (n + back > wave.size) wave = wave.copyOf(n + back)
        n += back
        finished = true
        val frames = if (n < frameLen) 0 else 1 + (n - frameLen) / shift
        val chunks = if (frames <= chunkFrames) 1 else (frames - chunkFrames + stepFrames - 1) / stepFrames + 1
        while (encoded.size < chunks) chunk(encoded.size)
    }

    private fun chunk(i: Int) {
        val from = step * i
        val samples = FloatArray(chunkSamples) { if (from + it < n) wave[from + it] else 0f }
        val fb = tensorOf(env, samples, longArrayOf(1, chunkSamples.toLong()))
        val rows = FloatArray(chunkFrames * mels)
        try {
            fbank.run(mapOf("samples" to fb)).use { r ->
                val buf = (r[0] as OnnxTensor).floatBuffer
                if (buf.remaining() < rows.size) throw IllegalStateException("bad_io")
                buf.get(rows)
            }
        } finally {
            fb.close()
        }
        val total = if (n < frameLen) 0 else 1 + (n - frameLen) / shift
        val valid = max(0, min(chunkFrames, total - stepFrames * i))
        for (k in valid * mels until rows.size) rows[k] = 0f
        val input = tensorOf(env, rows, longArrayOf(1, 1, chunkFrames.toLong(), mels.toLong()))
        val held = ArrayList<OnnxTensor>()
        try {
            val inputs = HashMap<String, OnnxTensor>()
            inputs["chunk"] = input
            for (name in state.keys()) {
                val spec = state.getJSONObject(name)
                val shape = PersonalPack.longs(spec.getJSONArray("shape"))
                val count = shape.fold(1L) { a, b -> a * b }.toInt()
                val t = if (spec.getString("dtype") == "i64") OnnxTensor.createTensor(env, LongBuffer.wrap(LongArray(count) { spec.getLong("fill") }), shape)
                else tensorOf(env, FloatArray(count) { spec.getDouble("fill").toFloat() }, shape)
                held.add(t)
                inputs[name] = t
            }
            enc.run(inputs).use { r ->
                val buf = (r[0] as OnnxTensor).floatBuffer
                if (buf.remaining() < frameFloats) throw IllegalStateException("bad_io")
                encoded.add(FloatArray(frameFloats).also { buf.get(it) })
            }
        } finally {
            input.close()
            for (t in held) t.close()
        }
    }

    override fun ended(i: Int) = finished && i >= encoded.size

    override fun feature(i: Int): FloatArray? {
        val half = window / 2
        if (!finished && encoded.size < i + half) return null
        val out = FloatArray(window * frameFloats)
        for (w in 0 until window) {
            val j = i - half + w
            if (j >= 0 && j < encoded.size) System.arraycopy(encoded[j], 0, out, w * frameFloats, frameFloats)
        }
        return out
    }
}

/**
 * Ultralight (wenet) or FeatherTalk on the phone: the package's models through ONNX Runtime, the template face prepared on the PC as the model input, a front
 * end that turns the voice into the audio input of each video frame, and the drawn face back as RGBA for the Dart side to lay over the template frame.
 * Frames are made on one worker thread and handed to `sink(gen, ms in the reply, rgba, ms it took)`.
 */
class PersonalEngine(private val sink: (Int, Int, ByteArray, Int) -> Unit, private val onError: (String) -> Unit) {
    private val env: OrtEnvironment = OrtEnvironment.getEnvironment()
    private var pack: PersonalPack? = null
    private val sessions = HashMap<String, OrtSession>()
    private var template = FloatArray(0)
    private val gen = AtomicInteger(0)
    private val queue = LinkedBlockingQueue<Cmd>()
    private var worker: Thread? = null
    private var reply: Reply? = null

    private sealed class Cmd(val gen: Int) {
        class Begin(gen: Int, val rate: Int) : Cmd(gen)
        class Feed(gen: Int, val pcm: ByteArray) : Cmd(gen)
        class Finish(gen: Int) : Cmd(gen)
    }

    private inner class Reply(rate: Int) {
        val resampler = Resampler(rate)
        val front: Frontend = newFrontend()
        var next = 0
        var work = 0L

        fun feed(pcm: ByteArray) {
            val t0 = System.nanoTime()
            val x = resampler.feed(pcm)
            front.add(x, x.size)
            work += System.nanoTime() - t0
        }

        fun finish() {
            val t0 = System.nanoTime()
            val x = resampler.finish()
            front.add(x, x.size)
            front.finish()
            work += System.nanoTime() - t0
        }
    }

    /** Opens the checked package and its models; the model inputs must be the ones `phone.json` says. */
    fun load(appRoot: File, dir: File, phoneSha: String, threads: Int): LoadResult {
        close()
        val (p, opened) = PersonalPack.open(appRoot, dir, phoneSha)
        if (p == null) return opened
        try {
            val models = p.json.getJSONObject("models")
            for (rel in models.keys()) {
                val opts = OrtSession.SessionOptions()
                opts.setIntraOpNumThreads(threads)
                opts.setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
                val s = env.createSession(p.file(rel).path, opts)
                sessions[rel] = s
                val want = models.getJSONObject(rel).getJSONObject("inputs")
                val have = s.inputInfo.mapValues { (it.value.info as? TensorInfo)?.shape }
                if (have.keys != want.keys().asSequence().toSet()) return failed(LoadResult("bad_io", "$rel takes ${have.keys}, phone.json says ${want.keys().asSequence().toList()}"))
                for ((name, shape) in have) {
                    val dims = PersonalPack.longs(want.getJSONArray(name))
                    if (shape == null || shape.size != dims.size || shape.indices.any { dims[it] > 0 && shape[it] > 0 && dims[it] != shape[it] }) return failed(LoadResult("bad_io", "$rel input $name has another shape"))
                }
            }
            val raw = p.file(p.json.getJSONObject("template").getString("input")).readBytes()
            if (raw.size != 6 * p.size * p.size) return failed(LoadResult("bad_pack", "template"))
            template = FloatArray(raw.size) { (raw[it].toInt() and 0xFF) / 255f }
            pack = p
            return LoadResult()
        } catch (e: Exception) {
            return failed(LoadResult("onnx_load_failed", e.message ?: e.javaClass.simpleName))
        }
    }

    private fun failed(r: LoadResult): LoadResult {
        for (s in sessions.values) s.close()
        sessions.clear()
        pack = null
        return r
    }

    fun newFrontend(): Frontend {
        val p = pack ?: throw IllegalStateException("not loaded")
        val f = p.front
        return when (f.getString("kind")) {
            "feather_hubert" -> FeatherFrontend(env, sessions.getValue(f.getString("file")), f)
            "wenet" -> WenetFrontend(env, sessions.getValue(f.getString("fbank_file")), sessions.getValue(f.getString("encoder_file")), f)
            else -> throw IllegalStateException("front end ${f.getString("kind")}")
        }
    }

    /** One inference: the drawn face as 3 x S x S floats (blue first), or null when the model gave something else. */
    fun infer(audio: FloatArray): FloatArray? {
        val p = pack ?: return null
        val s = sessions[p.modelFile] ?: return null
        val size = p.size.toLong()
        val a = tensorOf(env, audio, p.audioShape)
        val f = tensorOf(env, template, longArrayOf(1, 6, size, size))
        try {
            return s.run(mapOf("input" to f, "audio" to a)).use { result ->
                val buf = (result[0] as OnnxTensor).floatBuffer
                val n = 3 * p.size * p.size
                if (buf.remaining() < n) null else FloatArray(n).also { buf.get(it) }
            }
        } finally {
            a.close()
            f.close()
        }
    }

    /** The drawn face as RGBA bytes. */
    fun rgba(out: FloatArray): ByteArray {
        val n = (pack ?: return ByteArray(0)).size.let { it * it }
        val bytes = ByteArray(n * 4)
        for (i in 0 until n) {
            bytes[4 * i] = (out[2 * n + i] * 255f).toInt().coerceIn(0, 255).toByte()
            bytes[4 * i + 1] = (out[n + i] * 255f).toInt().coerceIn(0, 255).toByte()
            bytes[4 * i + 2] = (out[i] * 255f).toInt().coerceIn(0, 255).toByte()
            bytes[4 * i + 3] = 0xFF.toByte()
        }
        return bytes
    }

    // ---- a reply

    fun start(rate: Int): Int {
        val g = gen.incrementAndGet()
        queue.clear()
        queue.add(Cmd.Begin(g, rate))
        ensureWorker()
        return g
    }

    fun feed(pcm: ByteArray) {
        queue.add(Cmd.Feed(gen.get(), pcm))
    }

    fun finish() {
        queue.add(Cmd.Finish(gen.get()))
    }

    /** Drops what is queued; a frame being drawn is thrown away when it is done. */
    fun stop() {
        gen.incrementAndGet()
        queue.clear()
    }

    private fun ensureWorker() {
        if (worker?.isAlive == true) return
        worker = Thread {
            try {
                while (true) {
                    val c = queue.take()
                    if (c.gen != gen.get()) continue
                    try {
                        when (c) {
                            is Cmd.Begin -> reply = Reply(c.rate)
                            is Cmd.Feed -> reply?.feed(c.pcm)
                            is Cmd.Finish -> reply?.finish()
                        }
                    } catch (e: Exception) {
                        gen.incrementAndGet()
                        onError(if (e.message == "bad_io") "bad_io" else "inference_failed")
                        continue
                    }
                    drain(c.gen)
                }
            } catch (e: InterruptedException) {
                // the engine was closed
            }
        }.also {
            it.isDaemon = true
            it.start()
        }
    }

    private fun drain(g: Int) {
        val r = reply ?: return
        val p = pack ?: return
        while (g == gen.get()) {
            if (r.front.ended(r.next)) return
            val t0 = System.nanoTime()
            val audio = r.front.feature(r.next) ?: return
            val out = try {
                infer(audio)
            } catch (e: Exception) {
                gen.incrementAndGet()
                onError("inference_failed")
                return
            }
            if (out == null) {
                gen.incrementAndGet()
                onError("bad_io")
                return
            }
            val ms = ((System.nanoTime() - t0 + r.work) / 1_000_000L).toInt()
            r.work = 0
            val at = (r.next * 1000L / p.fps).toInt()
            r.next++
            if (g == gen.get()) sink(g, at, rgba(out), ms)
        }
    }

    fun close() {
        stop()
        worker?.interrupt()
        worker = null
        for (s in sessions.values) s.close()
        sessions.clear()
        pack = null
        reply = null
    }

    companion object {
        private fun step(name: String, ok: Boolean, detail: String) = mapOf("name" to name, "ok" to ok, "detail" to detail)

        /** What differs between the output and the summary the PC measured (empty: the same); the same test as phone_export.compare. */
        fun differences(ref: JSONObject, shape: LongArray, data: FloatBuffer, tol: JSONObject): List<String> {
            val want = PersonalPack.longs(ref.getJSONArray("shape"))
            if (!shape.contentEquals(want)) return listOf("shape ${shape.contentToString()} instead of ${want.contentToString()}")
            val n = data.remaining()
            val values = FloatArray(n).also { data.duplicate().get(it) }
            if (values.any { !it.isFinite() }) return listOf("not finite")
            val limit = tol.getDouble("atol") + tol.getDouble("rtol") * ref.getDouble("absmax")
            val out = ArrayList<String>()
            val at = ref.getJSONArray("at")
            val vals = ref.getJSONArray("values")
            for (k in 0 until at.length()) {
                val v = values[at.getInt(k)].toDouble()
                if (abs(v - vals.getDouble(k)) > limit) out.add("value ${at.getInt(k)}: $v instead of ${vals.getDouble(k)}")
            }
            var mean = 0.0
            for (v in values) mean += v
            mean /= n
            var varSum = 0.0
            for (v in values) varSum += (v - mean) * (v - mean)
            val std = sqrt(varSum / n)
            if (abs(mean - ref.getDouble("mean")) > limit) out.add("mean: $mean instead of ${ref.getDouble("mean")}")
            if (abs(std - ref.getDouble("std")) > limit) out.add("std: $std instead of ${ref.getDouble("std")}")
            return out
        }

        /** Runs one self-test stage of `phone.json` on the inputs it names and compares every output it lists. */
        private fun runStage(env: OrtEnvironment, p: PersonalPack, s: OrtSession, stage: JSONObject, tol: JSONObject): List<String> {
            val held = ArrayList<OnnxTensor>()
            try {
                val inputs = HashMap<String, OnnxTensor>()
                val specs = stage.getJSONArray("inputs")
                for (k in 0 until specs.length()) {
                    val spec = specs.getJSONObject(k)
                    val shape = PersonalPack.longs(spec.getJSONArray("shape"))
                    val count = shape.fold(1L) { a, b -> a * b }.toInt()
                    val t: OnnxTensor = when (spec.getString("dtype")) {
                        "i64" -> OnnxTensor.createTensor(env, LongBuffer.wrap(LongArray(count) { spec.getLong("fill") }), shape)
                        "u8" -> {
                            val raw = p.file(spec.getString("file")).readBytes()
                            val scale = spec.getDouble("scale").toFloat()
                            tensorOf(env, FloatArray(raw.size) { (raw[it].toInt() and 0xFF) / scale }, shape)
                        }
                        else -> tensorOf(env, if (spec.has("file")) floatsOf(p.file(spec.getString("file"))) else FloatArray(count) { spec.getDouble("fill").toFloat() }, shape)
                    }
                    held.add(t)
                    inputs[spec.getString("name")] = t
                }
                val bad = ArrayList<String>()
                s.run(inputs).use { r ->
                    val outs = stage.getJSONArray("outputs")
                    for (k in 0 until outs.length()) {
                        val ref = outs.getJSONObject(k)
                        val t = r[ref.getInt("index")] as OnnxTensor
                        bad.addAll(differences(ref, t.info.shape, t.floatBuffer, tol))
                    }
                }
                return bad
            } finally {
                for (t in held) t.close()
            }
        }

        /** Plays the synthetic audio of the package through the real front end and the model, twice (the first pass warms up); the time of the second is the speed. */
        private fun pipeline(engine: PersonalEngine, p: PersonalPack): Triple<Int, Double, Boolean> {
            val raw = floatsOf(p.file("phone/selftest/audio.f32"))
            val wenet = p.front.getString("kind") == "wenet"
            val x = FloatArray(raw.size) { if (wenet) raw[it] / 32767f else raw[it] * 0.2f }
            var frames = 0
            var ms = 0.0
            var finite = true
            for (pass in 0 until 2) {
                val front = engine.newFrontend()
                var i = 0
                val t0 = System.nanoTime()
                front.add(x, x.size)
                front.finish()
                while (!front.ended(i)) {
                    val out = engine.infer(front.feature(i) ?: break) ?: return Triple(i, 0.0, false)
                    if (out.any { !it.isFinite() }) finite = false
                    i++
                }
                frames = i
                ms = (System.nanoTime() - t0) / 1e6
            }
            return Triple(frames, if (frames > 0) ms / frames else 0.0, finite)
        }

        /**
         * The checks and the benchmark: the package's files are the ones listed, the models load with the inputs `phone.json` says, every stage of the self-test
         * gives the numbers the PC measured, and the synthetic audio goes through the whole path at what speed. It cannot judge how the lips look.
         */
        fun selfTest(appRoot: File, dir: File, phoneSha: String): Map<String, Any?> {
            val steps = ArrayList<Map<String, Any?>>()
            fun result(ok: Boolean, reason: String, ms: Double = 0.0, threads: Int = 0, frames: Int = 0, fps: Int = 0): Map<String, Any?> =
                mapOf("ok" to ok, "reason" to reason, "steps" to steps, "msPerFrame" to ms, "fps" to (if (ms > 0) 1000.0 / ms else 0.0), "threads" to threads, "frames" to frames, "requiredFps" to fps.toDouble())
            val engine = PersonalEngine({ _, _, _, _ -> }) { }
            try {
                val threads = min(4, max(2, Runtime.getRuntime().availableProcessors() / 2))
                val loaded = engine.load(appRoot, dir, phoneSha, threads)
                val p = engine.pack
                steps.add(step("package", loaded.ok, if (loaded.ok) "files and SHA-256 as listed, ${p?.engine}, ${p?.fps} frames/s, face ${p?.size} px" else "${loaded.reason}: ${loaded.detail}"))
                if (!loaded.ok || p == null) return result(false, loaded.reason, threads = threads)
                val tol = p.json.getJSONObject("selftest").getJSONObject("tolerance")
                val stages = p.json.getJSONObject("selftest").getJSONArray("stages")
                for (k in 0 until stages.length()) {
                    val stage = stages.getJSONObject(k)
                    val bad = runStage(engine.env, p, engine.sessions.getValue(stage.getString("model")), stage, tol)
                    steps.add(step(stage.getString("name"), bad.isEmpty(), if (bad.isEmpty()) "the numbers of the PC" else bad.take(3).joinToString("; ")))
                    if (bad.isNotEmpty()) return result(false, "numeric_mismatch", threads = threads, fps = p.fps)
                }
                val (frames, ms, finite) = pipeline(engine, p)
                steps.add(step("pipeline", frames > 0 && finite, "$frames frames from the synthetic audio, ${"%.1f".format(ms)} ms each, finite: $finite"))
                if (frames == 0 || !finite) return result(false, if (finite) "no_frames" else "output_not_finite", threads = threads, fps = p.fps)
                return result(true, "", ms, threads, frames, p.fps)
            } catch (e: Throwable) {
                steps.add(step("runtime", false, e.message ?: e.javaClass.simpleName))
                return result(false, if (e.message == "bad_io") "bad_io" else "onnx_load_failed")
            } finally {
                engine.close()
            }
        }
    }
}
