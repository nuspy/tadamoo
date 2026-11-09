package app.basalt.basalt_mobile

import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors
import kotlin.math.max
import kotlin.math.min

/**
 * The channels of Wav2Lip on the phone: `app.basalt/wav2lip` for the calls and `app.basalt/wav2lip/events` for what comes back (the
 * frames, the phone's thermal status, a failure of the runtime). Dart side: wav2lip_engine.dart.
 *
 * `load`, `selfTest` and the other slow calls run off the main thread; frames are handed to the event sink on the main thread.
 */
class Wav2LipChannel(private val context: Context, messenger: BinaryMessenger) : MethodChannel.MethodCallHandler, EventChannel.StreamHandler {
    private val main = Handler(Looper.getMainLooper())
    private val slow = Executors.newSingleThreadExecutor()
    private var sink: EventChannel.EventSink? = null
    private var engine: Wav2LipEngine? = null
    private var thermalOn = false

    private val thermalTick = object : Runnable {
        override fun run() {
            if (!thermalOn) return
            emit(mapOf("k" to "thermal", "level" to thermalStatus()))
            main.postDelayed(this, 3000)
        }
    }

    init {
        MethodChannel(messenger, "app.basalt/wav2lip").setMethodCallHandler(this)
        EventChannel(messenger, "app.basalt/wav2lip/events").setStreamHandler(this)
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink?) {
        sink = events
    }

    override fun onCancel(arguments: Any?) {
        sink = null
    }

    private fun emit(event: Map<String, Any?>) {
        main.post { sink?.success(event) }
    }

    /** Android's thermal status (0 none to 6 shutdown); 0 before Android 10, which does not say. */
    private fun thermalStatus(): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return 0
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.currentThermalStatus
    }

    private fun runtimePresent(): Boolean = try {
        ai.onnxruntime.OrtEnvironment.getEnvironment()
        true
    } catch (e: Throwable) {
        false
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "available" -> result.success(runtimePresent())
            "load" -> {
                val model = call.argument<String>("model")
                val face = call.argument<String>("face")
                if (model == null || face == null) {
                    result.error("args", "model and face are needed", null)
                    return
                }
                slow.execute {
                    val reply: Map<String, Any?> = try {
                        val pixels = Wav2LipEngine.readFace(face)
                        if (pixels == null) {
                            mapOf("ok" to false, "reason" to "no_face", "detail" to face)
                        } else {
                            val e = Wav2LipEngine({ gen, at, rgba -> emit(mapOf("k" to "frame", "g" to gen, "t" to at, "w" to 96, "h" to 96, "rgba" to rgba)) }, { why -> emit(mapOf("k" to "error", "why" to why)) })
                            val threads = min(4, max(2, Runtime.getRuntime().availableProcessors() / 2))
                            val loaded = e.load(model, pixels, threads)
                            if (loaded.ok) {
                                engine?.close()
                                engine = e
                            } else {
                                e.close()
                            }
                            mapOf("ok" to loaded.ok, "reason" to loaded.reason, "detail" to loaded.detail)
                        }
                    } catch (t: Throwable) {
                        mapOf("ok" to false, "reason" to "onnx_load_failed", "detail" to (t.message ?: t.javaClass.simpleName))
                    }
                    main.post { result.success(reply) }
                }
            }
            "selfTest" -> {
                val model = call.argument<String>("model")
                if (model == null) {
                    result.error("args", "model is needed", null)
                    return
                }
                val face = call.argument<String>("face")
                val fps = call.argument<Int>("fps") ?: 25
                slow.execute {
                    val reply: Map<String, Any?> = try {
                        Wav2LipEngine.selfTest(model, face, fps)
                    } catch (t: Throwable) {
                        mapOf("ok" to false, "reason" to "onnx_load_failed", "steps" to emptyList<Any>())
                    }
                    main.post { result.success(reply) }
                }
            }
            "start" -> {
                val e = engine
                if (e == null) {
                    result.error("not_loaded", "No model is loaded", null)
                    return
                }
                thermalOn = true
                main.removeCallbacks(thermalTick)
                main.post(thermalTick)
                result.success(e.start(call.argument<Int>("rate") ?: 24000, call.argument<Int>("fps") ?: 25))
            }
            "feed" -> {
                engine?.feed(call.arguments as ByteArray)
                result.success(null)
            }
            "finish" -> {
                engine?.finish()
                result.success(null)
            }
            "stop" -> {
                engine?.stop()
                thermalOn = false
                result.success(null)
            }
            "unload" -> {
                thermalOn = false
                engine?.close()
                engine = null
                result.success(null)
            }
            else -> result.notImplemented()
        }
    }

    /** The activity is going away. */
    fun release() {
        thermalOn = false
        engine?.close()
        engine = null
        slow.shutdown()
    }
}
