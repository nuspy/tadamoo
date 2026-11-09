package app.basalt.basalt_mobile

import android.Manifest
import android.app.Activity
import android.bluetooth.BluetoothAdapter
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Handler
import android.os.Looper
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * The channel `app.basalt/audio`: where the sound of a voice session goes (Bluetooth headset, loudspeaker, earpiece), the communication
 * audio mode while the session runs, the audio test, and the player of the PC's voice (CommPlayer). Dart side: audio_route_channel.dart.
 */
class AudioRouteChannel(private val activity: Activity, messenger: BinaryMessenger) : MethodChannel.MethodCallHandler {
    private val channel = MethodChannel(messenger, "app.basalt/audio")
    private val audio = activity.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val main = Handler(Looper.getMainLooper())
    private val player = CommPlayer()
    private var savedMode = AudioManager.MODE_NORMAL
    private var sessionOn = false
    private var pendingPermission: MethodChannel.Result? = null

    private val devices = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>?) = changed()
        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>?) = changed()
    }

    init {
        channel.setMethodCallHandler(this)
    }

    private fun changed() {
        channel.invokeMethod("devicesChanged", null)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        try {
            when (call.method) {
                "hasBluetoothPermission" -> result.success(bluetoothGranted())
                "requestBluetoothPermission" -> requestBluetooth(result)
                "startSession" -> {
                    startSession()
                    result.success(applyRoute(call.argument<String>("route") ?: "speaker"))
                }
                "setRoute" -> {
                    startSession()
                    result.success(applyRoute(call.argument<String>("route") ?: "speaker"))
                }
                "stopSession" -> {
                    stopSession()
                    result.success(null)
                }
                "probe" -> result.success(probe())
                "playBegin" -> {
                    player.begin(call.argument<Int>("rate") ?: 24000)
                    result.success(null)
                }
                "playAdd" -> {
                    player.add(call.arguments as ByteArray)
                    result.success(null)
                }
                "playGain" -> {
                    player.setGain((call.argument<Double>("gain") ?: 1.0).toFloat())
                    result.success(null)
                }
                "playPosition" -> result.success(player.positionMs())
                "playCancel" -> {
                    player.release()
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        } catch (e: Exception) {
            result.error("audio", e.message, null)
        }
    }

    // ---- the Bluetooth permission (Android 12 and later)

    private fun bluetoothGranted(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            activity.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

    private fun requestBluetooth(result: MethodChannel.Result) {
        if (bluetoothGranted()) {
            result.success(true)
            return
        }
        if (pendingPermission != null) {
            result.error("busy", "The permission window is already open", null)
            return
        }
        pendingPermission = result
        activity.requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), PERMISSION_CODE)
    }

    /** Called by the activity with the answer of the permission window. */
    fun onPermission(requestCode: Int, grantResults: IntArray) {
        if (requestCode != PERMISSION_CODE) return
        val result = pendingPermission ?: return
        pendingPermission = null
        result.success(grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED)
    }

    // ---- the session: communication mode and the route

    @Synchronized
    private fun startSession() {
        if (sessionOn) return
        savedMode = audio.mode
        audio.mode = AudioManager.MODE_IN_COMMUNICATION
        audio.registerAudioDeviceCallback(devices, main)
        sessionOn = true
    }

    @Synchronized
    private fun stopSession() {
        player.release()
        if (!sessionOn) return
        audio.unregisterAudioDeviceCallback(devices)
        clearRoute()
        audio.mode = savedMode
        sessionOn = false
    }

    /** Frees what the session holds (the activity is going away). */
    fun release() = stopSession()

    private fun ok(device: String = "") = mapOf("reason" to "", "device" to device)

    private fun fail(reason: String) = mapOf("reason" to reason)

    private fun applyRoute(route: String): Map<String, Any?> = when (route) {
        "bluetooth" -> useBluetooth()
        "earpiece" -> useBuiltin(AudioDeviceInfo.TYPE_BUILTIN_EARPIECE, false)
        else -> useBuiltin(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER, true)
    }

    @Suppress("DEPRECATION")
    private fun useBluetooth(): Map<String, Any?> {
        if (!bluetoothGranted()) return fail("permission")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val device = audio.availableCommunicationDevices.firstOrNull {
                it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || it.type == AudioDeviceInfo.TYPE_BLE_HEADSET
            } ?: return fail("no_device")
            return if (audio.setCommunicationDevice(device)) ok(device.productName?.toString() ?: "") else fail("failed")
        }
        val present = audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any {
            it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
        }
        if (!present || !audio.isBluetoothScoAvailableOffCall) return fail("no_device")
        audio.isSpeakerphoneOn = false
        audio.startBluetoothSco()
        audio.isBluetoothScoOn = true
        return ok()
    }

    @Suppress("DEPRECATION")
    private fun useBuiltin(type: Int, speaker: Boolean): Map<String, Any?> {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val device = audio.availableCommunicationDevices.firstOrNull { it.type == type }
            if (device == null) return if (speaker) { audio.isSpeakerphoneOn = true; ok() } else fail("no_device")
            return if (audio.setCommunicationDevice(device)) ok() else fail("failed")
        }
        if (audio.isBluetoothScoOn) {
            audio.stopBluetoothSco()
            audio.isBluetoothScoOn = false
        }
        audio.isSpeakerphoneOn = speaker
        return ok()
    }

    @Suppress("DEPRECATION")
    private fun clearRoute() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audio.clearCommunicationDevice()
        } else if (audio.isBluetoothScoOn) {
            audio.stopBluetoothSco()
            audio.isBluetoothScoOn = false
        }
        audio.isSpeakerphoneOn = false
    }

    // ---- the audio test

    private fun typeName(type: Int): String = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "builtin earpiece"
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "builtin speaker"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth sco"
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth a2dp"
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired headset"
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "wired headphones"
        AudioDeviceInfo.TYPE_USB_HEADSET -> "usb headset"
        AudioDeviceInfo.TYPE_USB_DEVICE -> "usb device"
        AudioDeviceInfo.TYPE_HEARING_AID -> "hearing aid"
        else -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && type == AudioDeviceInfo.TYPE_BLE_HEADSET) "ble headset" else "type $type"
    }

    private fun modeName(mode: Int): String = when (mode) {
        AudioManager.MODE_NORMAL -> "normal"
        AudioManager.MODE_RINGTONE -> "ringtone"
        AudioManager.MODE_IN_CALL -> "in call"
        AudioManager.MODE_IN_COMMUNICATION -> "in communication"
        else -> "mode $mode"
    }

    /** Whether the echo canceller can be attached to a communication capture, which needs the microphone permission. */
    private fun echoOnCapture(): String {
        if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) return "unknown (microphone permission not given)"
        if (!AcousticEchoCanceler.isAvailable()) return "no"
        var rec: AudioRecord? = null
        return try {
            val size = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            rec = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, 16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size * 2)
            val aec = AcousticEchoCanceler.create(rec.audioSessionId)
            val attached = aec != null
            aec?.release()
            if (attached) "yes" else "no"
        } catch (e: Exception) {
            "unknown (${e.javaClass.simpleName})"
        } finally {
            rec?.release()
        }
    }

    private fun bluetoothState(): String = try {
        val adapter = BluetoothAdapter.getDefaultAdapter()
        if (adapter == null) "no adapter" else if (adapter.isEnabled) "on" else "off"
    } catch (e: SecurityException) {
        "unknown (permission)"
    }

    @Suppress("DEPRECATION")
    private fun probe(): Map<String, Any?> {
        val list: List<AudioDeviceInfo> = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && bluetoothGranted()) {
            audio.availableCommunicationDevices
        } else {
            audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS).toList()
        }
        val current = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audio.communicationDevice?.let { typeName(it.type) + (it.productName?.toString()?.let { n -> " \"$n\"" } ?: "") } ?: ""
        } else {
            "speakerphone ${if (audio.isSpeakerphoneOn) "on" else "off"}, bluetooth sco ${if (audio.isBluetoothScoOn) "on" else "off"}"
        }
        return mapOf(
            "android" to "${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
            "model" to "${Build.MANUFACTURER} ${Build.MODEL}",
            "aec" to AcousticEchoCanceler.isAvailable(),
            "ns" to NoiseSuppressor.isAvailable(),
            "agc" to AutomaticGainControl.isAvailable(),
            "aecOnCapture" to echoOnCapture(),
            "btPermission" to if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) "not needed before Android 12" else if (bluetoothGranted()) "granted" else "not granted",
            "btOn" to bluetoothState(),
            "mode" to modeName(audio.mode),
            "current" to current,
            "devices" to list.map { mapOf("type" to typeName(it.type), "name" to (it.productName?.toString() ?: "")) },
        )
    }

    companion object {
        private const val PERMISSION_CODE = 4712
    }
}
