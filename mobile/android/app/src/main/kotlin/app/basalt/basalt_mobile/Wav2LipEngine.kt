package app.basalt.basalt_mobile

import ai.onnxruntime.OnnxJavaType
import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import ai.onnxruntime.TensorInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.nio.FloatBuffer
import java.util.Random
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * The mel spectrogram Wav2Lip is trained on, computed as the voice arrives: 16 kHz mono, pre-emphasis 0.97, a periodic Hann window of 800
 * samples, a hop of 200, magnitudes (not powers) through 80 Slaney mel bands from 55 to 7600 Hz, 20 log10 with a floor of 1e-5, minus
 * 20 dB, then scaled so that -100 dB is -4 and 0 dB is 4, clipped to that range. The start is padded with 400 zeros where librosa would
 * reflect, because the voice of a reply starts after silence.
 */
class MelExtractor {
    private var padded = FloatArray(16384)
    private var length = 0
    private var prev = 0f
    private var next = 0
    private val xw = FloatArray(NFFT)
    private val mag = FloatArray(BINS)
    val frames = ArrayList<FloatArray>()

    init {
        reset()
    }

    fun reset() {
        padded = FloatArray(16384)
        length = NFFT / 2
        prev = 0f
        next = 0
        frames.clear()
    }

    /** Appends `count` samples of 16 kHz audio in [-1, 1] and computes every frame they complete. */
    fun add(x: FloatArray, count: Int) {
        if (length + count > padded.size) padded = padded.copyOf(max(padded.size * 2, length + count))
        for (i in 0 until count) {
            val v = x[i]
            padded[length++] = v - PREEMPH * prev
            prev = v
        }
        while (next * HOP + NFFT <= length) {
            frames.add(frame(next))
            next++
        }
    }

    fun pad(zeros: Int) = add(FloatArray(zeros), zeros)

    private fun frame(k: Int): FloatArray {
        val base = k * HOP
        for (n in 0 until NFFT) xw[n] = padded[base + n] * WINDOW[n]
        for (b in 0 until BINS) {
            var re = 0f
            var im = 0f
            val off = b * NFFT
            for (n in 0 until NFFT) {
                val v = xw[n]
                re += v * COS[off + n]
                im += v * SIN[off + n]
            }
            mag[b] = sqrt(re * re + im * im)
        }
        val out = FloatArray(NMELS)
        for (m in 0 until NMELS) {
            var acc = 0f
            val f = FILTERS[m]
            for (b in LO[m]..HI[m]) acc += f[b] * mag[b]
            val db = 20f * log10(max(1e-5f, acc)) - REF_DB
            out[m] = ((2f * MAX_ABS) * ((db - MIN_DB) / (-MIN_DB)) - MAX_ABS).coerceIn(-MAX_ABS, MAX_ABS)
        }
        return out
    }

    companion object {
        const val SR = 16000
        const val NFFT = 800
        const val HOP = 200
        const val NMELS = 80
        const val BINS = NFFT / 2 + 1
        const val PREEMPH = 0.97f
        const val REF_DB = 20f
        const val MIN_DB = -100f
        const val MAX_ABS = 4f
        const val FMIN = 55.0
        const val FMAX = 7600.0

        val WINDOW: FloatArray by lazy { FloatArray(NFFT) { (0.5 - 0.5 * cos(2.0 * PI * it / NFFT)).toFloat() } }
        val COS: FloatArray by lazy { FloatArray(BINS * NFFT) { cos(2.0 * PI * ((it / NFFT).toLong() * (it % NFFT) % NFFT) / NFFT).toFloat() } }
        val SIN: FloatArray by lazy { FloatArray(BINS * NFFT) { sin(2.0 * PI * ((it / NFFT).toLong() * (it % NFFT) % NFFT) / NFFT).toFloat() } }

        private const val F_SP = 200.0 / 3.0
        private const val MIN_LOG_HZ = 1000.0
        private const val MIN_LOG_MEL = MIN_LOG_HZ / F_SP
        private val LOG_STEP = ln(6.4) / 27.0

        private fun hzToMel(f: Double) = if (f >= MIN_LOG_HZ) MIN_LOG_MEL + ln(f / MIN_LOG_HZ) / LOG_STEP else f / F_SP
        private fun melToHz(m: Double) = if (m >= MIN_LOG_MEL) MIN_LOG_HZ * exp(LOG_STEP * (m - MIN_LOG_MEL)) else F_SP * m

        /** The centres of the bands plus one edge on each side, in Hz. */
        val EDGES: DoubleArray by lazy {
            val lo = hzToMel(FMIN)
            val hi = hzToMel(FMAX)
            DoubleArray(NMELS + 2) { melToHz(lo + (hi - lo) * it / (NMELS + 1)) }
        }

        val FILTERS: Array<FloatArray> by lazy {
            Array(NMELS) { i ->
                FloatArray(BINS) { k ->
                    val hz = k * SR.toDouble() / NFFT
                    val lower = (hz - EDGES[i]) / (EDGES[i + 1] - EDGES[i])
                    val upper = (EDGES[i + 2] - hz) / (EDGES[i + 2] - EDGES[i + 1])
                    (max(0.0, min(lower, upper)) * 2.0 / (EDGES[i + 2] - EDGES[i])).toFloat()
                }
            }
        }

        val LO: IntArray by lazy { IntArray(NMELS) { m -> FILTERS[m].indexOfFirst { it > 0f }.let { if (it < 0) 0 else it } } }
        val HI: IntArray by lazy { IntArray(NMELS) { m -> FILTERS[m].indexOfLast { it > 0f }.let { if (it < 0) 0 else it } } }
    }
}

/** One reply: its voice at some sample rate, resampled to 16 kHz and turned into mel frames, from which the video frames are cut. */
class ReplyStream(val gen: Int, private val rate: Int, val fps: Int) {
    val mel = MelExtractor()
    private val ratio = rate / MelExtractor.SR.toDouble()
    private var src = FloatArray(MelExtractor.SR * 2)
    private var srcLen = 0
    private var outIdx = 0L
    private var inSamples = 0L
    private var finished = false
    var nextFrame = 0
        private set

    fun feed(pcm: ByteArray) {
        val n = pcm.size / 2
        if (srcLen + n + 1 > src.size) src = src.copyOf(max(src.size * 2, srcLen + n + 1))
        for (i in 0 until n) {
            val s = ((pcm[2 * i + 1].toInt() shl 8) or (pcm[2 * i].toInt() and 0xFF)).toShort()
            src[srcLen++] = s / 32768f
        }
        inSamples += n
        resample()
    }

    private fun resample() {
        val out = FloatArray(max(1, ((srcLen - outIdx * ratio) / ratio).toInt() + 2))
        var k = 0
        while (outIdx * ratio + 1 < srcLen) {
            val pos = outIdx * ratio
            val i = pos.toInt()
            val frac = (pos - i).toFloat()
            out[k++] = src[i] * (1f - frac) + src[i + 1] * frac
            outIdx++
        }
        if (k > 0) mel.add(out, k)
    }

    /** No more voice: what is left is drawn against silence. */
    fun finish() {
        if (finished) return
        finished = true
        if (srcLen + 1 > src.size) src = src.copyOf(src.size + 1)
        src[srcLen++] = 0f
        resample()
        mel.pad(MelExtractor.NFFT / 2 + (MEL_STEP + 1) * MelExtractor.HOP)
    }

    private fun start() = (80.0 * nextFrame / fps).toInt()

    /** The next video frame's 80 x 16 mel window (row-major, band by time) and its time in ms, or null when the voice has not got that far. */
    fun next(): Pair<Int, FloatArray>? {
        if (finished && nextFrame * 1000L / fps >= inSamples * 1000L / rate) return null
        val s = start()
        if (mel.frames.size < s + MEL_STEP) return null
        val chunk = FloatArray(MelExtractor.NMELS * MEL_STEP)
        for (t in 0 until MEL_STEP) {
            val f = mel.frames[s + t]
            for (m in 0 until MelExtractor.NMELS) chunk[m * MEL_STEP + t] = f[m]
        }
        val at = (nextFrame * 1000L / fps).toInt()
        nextFrame++
        return Pair(at, chunk)
    }

    companion object {
        const val MEL_STEP = 16
    }
}

/** What loading a model found. `reason` is empty when it worked. */
class LoadResult(val reason: String = "", val detail: String = "") {
    val ok get() = reason.isEmpty()
}

/**
 * Wav2Lip on the phone: the ONNX model through ONNX Runtime on the 96 x 96 straightened face (its lower half hidden, next to the whole
 * face as reference), a mel window per video frame, and the drawn face back as RGBA. Frames are made on one worker thread and handed to
 * `sink(gen, ms, rgba)` with the time in the reply they belong to.
 */
class Wav2LipEngine(private val sink: (Int, Int, ByteArray) -> Unit, private val onError: (String) -> Unit) {
    private val env: OrtEnvironment = OrtEnvironment.getEnvironment()
    private var session: OrtSession? = null
    private var melName = ""
    private var faceName = ""
    private var face = FloatArray(0)
    private val gen = AtomicInteger(0)
    private val queue = LinkedBlockingQueue<Cmd>()
    private var worker: Thread? = null
    private var stream: ReplyStream? = null

    private sealed class Cmd(val gen: Int) {
        class Begin(gen: Int, val rate: Int, val fps: Int) : Cmd(gen)
        class Feed(gen: Int, val pcm: ByteArray) : Cmd(gen)
        class Finish(gen: Int) : Cmd(gen)
    }

    /** Opens the model and finds which input is the mel window and which is the face; `faceRgb` is the 96 x 96 face as ARGB pixels. */
    fun load(modelPath: String, faceArgb: IntArray, threads: Int): LoadResult {
        close()
        try {
            val opts = OrtSession.SessionOptions()
            opts.setIntraOpNumThreads(threads)
            opts.setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
            val s = env.createSession(modelPath, opts)
            var mel = ""
            var fc = ""
            val shapes = StringBuilder()
            for ((name, info) in s.inputInfo) {
                val t = info.info as? TensorInfo ?: continue
                shapes.append(name).append(t.shape.contentToString()).append(' ')
                if (t.type != OnnxJavaType.FLOAT) {
                    s.close()
                    return LoadResult("bad_io", "input $name is not float32")
                }
                val sh = t.shape
                if (sh.size == 4 && sh[2] == 80L && sh[3] == 16L) mel = name
                if (sh.size == 4 && sh[1] == 6L && sh[2] == 96L && sh[3] == 96L) fc = name
            }
            if (mel.isEmpty() || fc.isEmpty()) {
                s.close()
                return LoadResult("bad_io", "expected a mel input [N,1,80,16] and a face input [N,6,96,96], found $shapes")
            }
            session = s
            melName = mel
            faceName = fc
            face = faceInput(faceArgb)
            return LoadResult()
        } catch (e: Exception) {
            return LoadResult("onnx_load_failed", e.message ?: e.javaClass.simpleName)
        }
    }

    /** The face input: the face with its lower half hidden, then the whole face, as 6 x 96 x 96 floats in [0, 1], blue first as in OpenCV. */
    private fun faceInput(argb: IntArray): FloatArray {
        val n = SIZE * SIZE
        val out = FloatArray(6 * n)
        for (i in 0 until n) {
            val p = argb[i]
            val b = (p and 0xFF) / 255f
            val g = ((p shr 8) and 0xFF) / 255f
            val r = ((p shr 16) and 0xFF) / 255f
            val masked = (i / SIZE) < SIZE / 2
            out[i] = if (masked) b else 0f
            out[n + i] = if (masked) g else 0f
            out[2 * n + i] = if (masked) r else 0f
            out[3 * n + i] = b
            out[4 * n + i] = g
            out[5 * n + i] = r
        }
        return out
    }

    /** One inference: the drawn face as 3 x 96 x 96 floats (blue first), or null when the model gave something else. */
    fun infer(mel: FloatArray): FloatArray? {
        val s = session ?: return null
        val m = OnnxTensor.createTensor(env, FloatBuffer.wrap(mel), longArrayOf(1, 1, 80, 16))
        val f = OnnxTensor.createTensor(env, FloatBuffer.wrap(face), longArrayOf(1, 6, SIZE.toLong(), SIZE.toLong()))
        try {
            return s.run(mapOf(melName to m, faceName to f)).use { result ->
                val buf = (result[0] as OnnxTensor).floatBuffer
                if (buf.remaining() < 3 * SIZE * SIZE) null else FloatArray(3 * SIZE * SIZE).also { buf.get(it) }
            }
        } finally {
            m.close()
            f.close()
        }
    }

    /** The drawn face as RGBA bytes; the model's range (0 to 1, or 0 to 255) is told by its largest value. */
    fun rgba(out: FloatArray): ByteArray {
        val n = SIZE * SIZE
        var top = 0f
        for (v in out) if (v > top) top = v
        val scale = if (top > 1.5f) 1f else 255f
        val bytes = ByteArray(n * 4)
        for (i in 0 until n) {
            bytes[4 * i] = (out[2 * n + i] * scale).toInt().coerceIn(0, 255).toByte()
            bytes[4 * i + 1] = (out[n + i] * scale).toInt().coerceIn(0, 255).toByte()
            bytes[4 * i + 2] = (out[i] * scale).toInt().coerceIn(0, 255).toByte()
            bytes[4 * i + 3] = 0xFF.toByte()
        }
        return bytes
    }

    // ---- a reply

    fun start(rate: Int, fps: Int): Int {
        val g = gen.incrementAndGet()
        queue.clear()
        queue.add(Cmd.Begin(g, rate, fps))
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
                    when (c) {
                        is Cmd.Begin -> stream = ReplyStream(c.gen, c.rate, c.fps)
                        is Cmd.Feed -> stream?.feed(c.pcm)
                        is Cmd.Finish -> stream?.finish()
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
        val st = stream ?: return
        while (g == gen.get()) {
            val (at, mel) = st.next() ?: return
            val out = try {
                infer(mel)
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
            if (g == gen.get()) sink(g, at, rgba(out))
        }
    }

    fun close() {
        stop()
        worker?.interrupt()
        worker = null
        session?.close()
        session = null
    }

    companion object {
        const val SIZE = 96

        /** The face of a PNG file as 96 x 96 ARGB pixels, or null when it cannot be read. */
        fun readFace(path: String): IntArray? {
            val bmp: Bitmap = BitmapFactory.decodeFile(path) ?: return null
            val scaled = if (bmp.width == SIZE && bmp.height == SIZE) bmp else Bitmap.createScaledBitmap(bmp, SIZE, SIZE, true)
            val px = IntArray(SIZE * SIZE)
            scaled.getPixels(px, 0, SIZE, 0, 0, SIZE, SIZE)
            return px
        }

        /** A face of the test's own, for when there is no avatar: a smooth gradient. */
        fun syntheticFace(): IntArray = IntArray(SIZE * SIZE) {
            val x = it % SIZE
            val y = it / SIZE
            val r = (160 + 40 * sin(x / 10.0)).toInt()
            val g = (120 + 30 * cos(y / 12.0)).toInt()
            val b = (100 + (x + y) / 4)
            (0xFF shl 24) or (r shl 16) or (g shl 8) or b
        }

        private fun step(name: String, ok: Boolean, detail: String) = mapOf("name" to name, "ok" to ok, "detail" to detail)

        /**
         * The checks and the benchmark: the model loads and has the inputs expected; the mel of silence is the floor and a tone lands in
         * the band of its frequency; the model's output keeps the upper half of the face it was given; and the speed of a frame (the
         * mel window and the model) over 40 frames. It cannot judge how the lips look.
         */
        fun selfTest(modelPath: String, facePath: String?, fps: Int): Map<String, Any?> {
            val steps = ArrayList<Map<String, Any?>>()
            fun result(ok: Boolean, reason: String, ms: Double = 0.0, threads: Int = 0, frames: Int = 0): Map<String, Any?> =
                mapOf("ok" to ok, "reason" to reason, "steps" to steps, "msPerFrame" to ms, "fps" to (if (ms > 0) 1000.0 / ms else 0.0), "threads" to threads, "frames" to frames)

            val sink: (Int, Int, ByteArray) -> Unit = { _, _, _ -> }
            val engine = Wav2LipEngine(sink) { }
            try {
                val threads = min(4, max(2, Runtime.getRuntime().availableProcessors() / 2))
                val argb = (if (facePath != null) readFace(facePath) else null) ?: syntheticFace()
                steps.add(step("face", true, if (facePath != null) "the avatar's face" else "a face of the test's own"))
                val loaded = engine.load(modelPath, argb, threads)
                steps.add(step("model", loaded.ok, if (loaded.ok) "loaded, mel input ${engine.melName}, face input ${engine.faceName}" else "${loaded.reason}: ${loaded.detail}"))
                if (!loaded.ok) return result(false, loaded.reason, threads = threads)

                // the mel of silence is the floor, and a 1 kHz tone is in the band of 1 kHz
                val silent = MelExtractor()
                silent.pad(4000)
                val floorOk = silent.frames.isNotEmpty() && silent.frames[2].all { abs(it + 4f) < 1e-3f }
                val tone = MelExtractor()
                val wave = FloatArray(8000) { (0.5 * sin(2.0 * PI * 1000.0 * it / MelExtractor.SR)).toFloat() }
                tone.add(wave, wave.size)
                val row = tone.frames[tone.frames.size / 2]
                var peak = 0
                for (m in row.indices) if (row[m] > row[peak]) peak = m
                var expected = 0
                for (m in 0 until MelExtractor.NMELS) if (abs(MelExtractor.EDGES[m + 1] - 1000.0) < abs(MelExtractor.EDGES[expected + 1] - 1000.0)) expected = m
                val toneOk = abs(peak - expected) <= 1
                steps.add(step("mel", floorOk && toneOk, "silence at the floor: $floorOk, 1 kHz in band $peak (expected $expected)"))
                if (!(floorOk && toneOk)) return result(false, "mel_mismatch", threads = threads)

                // the output keeps the upper half of the input face
                val quiet = FloatArray(MelExtractor.NMELS * ReplyStream.MEL_STEP) { -4f }
                val out = engine.infer(quiet)
                if (out == null) {
                    steps.add(step("output", false, "the model's output is not 3 x 96 x 96"))
                    return result(false, "bad_io", threads = threads)
                }
                var top = 0f
                for (v in out) if (v > top) top = v
                val scale = if (top > 1.5f) 1f else 255f
                var diff = 0.0
                var count = 0
                val n = SIZE * SIZE
                for (c in 0 until 3) {
                    for (i in 0 until n / 2) {
                        diff += abs(out[c * n + i] * scale / 255.0 - engine.face[(3 + c) * n + i])
                        count++
                    }
                }
                val mean = diff / count
                val finite = out.all { it.isFinite() }
                steps.add(step("output", finite && mean < 0.15, "range 0 to ${"%.2f".format(top)}, upper half differs from the input face by ${"%.3f".format(mean)} (limit 0.150)"))
                if (!(finite && mean < 0.15)) return result(false, "output_unlike_input", threads = threads)

                // speed: the mel windows of a second of noise and 40 inferences
                val rnd = Random(7)
                val noise = FloatArray(MelExtractor.SR * 2) { (rnd.nextGaussian() * 0.1).toFloat() }
                val mel = MelExtractor()
                val m0 = System.nanoTime()
                mel.add(noise, noise.size)
                val melMsPerMel = (System.nanoTime() - m0) / 1e6 / max(1, mel.frames.size)
                val melMsPerFrame = melMsPerMel * 80.0 / fps
                val windows = ArrayList<FloatArray>()
                for (k in 0 until 40) {
                    val s = (k * 3.2).toInt()
                    val w = FloatArray(MelExtractor.NMELS * ReplyStream.MEL_STEP)
                    for (t in 0 until ReplyStream.MEL_STEP) for (m in 0 until MelExtractor.NMELS) w[m * ReplyStream.MEL_STEP + t] = mel.frames[s + t][m]
                    windows.add(w)
                }
                for (k in 0 until 3) engine.infer(windows[k])
                val t0 = System.nanoTime()
                for (w in windows) engine.infer(w)
                val inferMs = (System.nanoTime() - t0) / 1e6 / windows.size
                val ms = inferMs + melMsPerFrame
                steps.add(step("speed", true, "${"%.1f".format(inferMs)} ms for the model and ${"%.1f".format(melMsPerFrame)} ms for the mel window of a frame"))
                return result(true, "", ms, threads, windows.size)
            } catch (e: Throwable) {
                steps.add(step("runtime", false, e.message ?: e.javaClass.simpleName))
                return result(false, "onnx_load_failed")
            } finally {
                engine.close()
            }
        }
    }
}
