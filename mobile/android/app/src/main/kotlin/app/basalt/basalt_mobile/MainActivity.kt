package app.basalt.basalt_mobile

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

class MainActivity : FlutterActivity() {
    private val request = 4711
    private var pending: MethodChannel.Result? = null
    private var audioRoute: AudioRouteChannel? = null
    private var wav2lip: Wav2LipChannel? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        audioRoute = AudioRouteChannel(this, flutterEngine.dartExecutor.binaryMessenger)
        wav2lip = Wav2LipChannel(this, flutterEngine.dartExecutor.binaryMessenger)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "app.basalt/picker").setMethodCallHandler { call, result ->
            if (call.method != "pickDocuments") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            if (pending != null) {
                result.error("busy", "A window is already open", null)
                return@setMethodCallHandler
            }
            pending = result
            val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE)
                type = "*/*"
                putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                // the same window as for the phone's files, opened on Basalt's entry (the PC's shared folders)
                if (call.argument<Boolean>("basalt") == true && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    putExtra(DocumentsContract.EXTRA_INITIAL_URI, BasaltDocumentsProvider.rootUri())
                }
            }
            startActivityForResult(intent, request)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        audioRoute?.onPermission(requestCode, grantResults)
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }

    override fun onDestroy() {
        audioRoute?.release()
        wav2lip?.release()
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode != request) {
            @Suppress("DEPRECATION")
            super.onActivityResult(requestCode, resultCode, data)
            return
        }
        val result = pending ?: return
        pending = null
        if (resultCode != Activity.RESULT_OK || data == null) {
            result.success(emptyList<Map<String, String>>())
            return
        }
        val uris = mutableListOf<Uri>()
        data.clipData?.let { for (i in 0 until it.itemCount) uris.add(it.getItemAt(i).uri) }
        data.data?.let { if (uris.isEmpty()) uris.add(it) }
        Thread {
            val out = mutableListOf<Map<String, String>>()
            try {
                val dir = File(cacheDir, "picked")
                dir.mkdirs()
                for (uri in uris) {
                    var name = "file"
                    contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                        if (c.moveToFirst()) name = c.getString(0) ?: name
                    }
                    val target = File(dir, "${System.nanoTime()}_$name")
                    contentResolver.openInputStream(uri)?.use { input -> target.outputStream().use { input.copyTo(it) } }
                    out.add(mapOf("name" to name, "path" to target.absolutePath))
                }
                runOnUiThread { result.success(out) }
            } catch (e: Exception) {
                runOnUiThread { result.error("copy", e.message, null) }
            }
        }.start()
    }
}
