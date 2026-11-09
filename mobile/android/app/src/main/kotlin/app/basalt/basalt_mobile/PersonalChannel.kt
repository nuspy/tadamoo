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
import java.io.File
import java.util.concurrent.Executors
import kotlin.math.max
import kotlin.math.min

/**
 * The channels of the personalised engines (Ultralight, FeatherTalk) on the phone: `app.basalt/personal` for the calls and `app.basalt/personal/events` for
 * what comes back (the frames with the time each took, the phone's thermal status, a failure of the runtime). Dart side: personal_engine.dart.
 *
 * `load` and `selfTest` take the folder of an engine's package inside the app's own folder and the SHA-256 of its `phone.json` from the avatar's manifest;
 * PersonalPack checks every file again before anything is opened. They run off the main thread; frames are handed to the event sink on the main thread.
 */
class PersonalChannel(private val context: Context, messenger: BinaryMessenger) : MethodChannel.MethodCallHandler, EventChannel.StreamHandler {
    private val main = Handler(Looper.getMainLooper())
    private val slow = Executors.newSingleThreadExecutor()
    private var sink: EventChannel.EventSink? = null
    private var engine: PersonalEngine? = null
    private var thermalOn = false
    private val appRoot: File get() = context.filesDir.parentFile ?: context.filesDir

    private val thermalTick = object : Runnable {
        override fun run() {
            if (!thermalOn) return
            emit(mapOf("k" to "thermal", "level" to thermalStatus()))
            main.postDelayed(this, 3000)
        }
    }

    init {
        MethodChannel(messenger, "app.basalt/personal").setMethodCallHandler(this)
        EventChannel(messenger, "app.basalt/personal/events").setStreamHandler(this)
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
                val dir = call.argument<String>("dir")
                val sha = call.argument<String>("sha")
                if (dir == null || sha == null) {
                    result.error("args", "dir and sha are needed", null)
                    return
                }
                slow.execute {
                    val reply: Map<String, Any?> = try {
                        val e = PersonalEngine({ gen, at, rgba, ms ->
                            val side = Math.round(Math.sqrt(rgba.size / 4.0)).toInt()
                            emit(mapOf("k" to "frame", "g" to gen, "t" to at, "w" to side, "h" to side, "rgba" to rgba, "ms" to ms))
                        }, { why -> emit(mapOf("k" to "error", "why" to why)) })
                        val threads = min(4, max(2, Runtime.getRuntime().availableProcessors() / 2))
                        val loaded = e.load(appRoot, File(dir), sha, threads)
                        if (loaded.ok) {
                            engine?.close()
                            engine = e
                        } else {
                            e.close()
                        }
                        mapOf("ok" to loaded.ok, "reason" to loaded.reason, "detail" to loaded.detail)
                    } catch (t: Throwable) {
                        mapOf("ok" to false, "reason" to "onnx_load_failed", "detail" to (t.message ?: t.javaClass.simpleName))
                    }
                    main.post { result.success(reply) }
                }
            }
            "selfTest" -> {
                val dir = call.argument<String>("dir")
                val sha = call.argument<String>("sha")
                if (dir == null || sha == null) {
                    result.error("args", "dir and sha are needed", null)
                    return
                }
                slow.execute {
                    val reply: Map<String, Any?> = try {
                        PersonalEngine.selfTest(appRoot, File(dir), sha)
                    } catch (t: Throwable) {
                        mapOf("ok" to false, "reason" to "onnx_load_failed", "steps" to emptyList<Any>())
                    }
                    main.post { result.success(reply) }
                }
            }
            "start" -> {
                val e = engine
                if (e == null) {
                    result.error("not_loaded", "No package is loaded", null)
                    return
                }
                thermalOn = true
                main.removeCallbacks(thermalTick)
                main.post(thermalTick)
                result.success(e.start(call.argument<Int>("rate") ?: 24000))
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
