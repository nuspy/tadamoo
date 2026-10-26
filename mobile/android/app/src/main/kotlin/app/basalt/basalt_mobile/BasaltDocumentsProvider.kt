package app.basalt.basalt_mobile

import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.provider.DocumentsContract.Root
import android.provider.DocumentsProvider
import android.webkit.MimeTypeMap
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.ConcurrentHashMap

/**
 * The PC's shared folders as an entry of Android's own document window ("Basalt - PC"), so a file on the PC is chosen the way a file on the phone is.
 * It reads what the app keeps of the server (address and key) and asks the PC's /api/files routes; nothing is written to the PC.
 */
class BasaltDocumentsProvider : DocumentsProvider() {
    companion object {
        const val AUTHORITY = "app.basalt.basalt_mobile.documents"
        const val ROOT_DOC = "root"

        fun rootUri(): Uri = DocumentsContract.buildDocumentUri(AUTHORITY, ROOT_DOC)
    }

    private class Entry(val id: String, val name: String, val dir: Boolean, val size: Long, val modified: Long)

    private val known = ConcurrentHashMap<String, Entry>()
    private val rootProjection = arrayOf(Root.COLUMN_ROOT_ID, Root.COLUMN_FLAGS, Root.COLUMN_ICON, Root.COLUMN_TITLE, Root.COLUMN_SUMMARY, Root.COLUMN_DOCUMENT_ID, Root.COLUMN_MIME_TYPES)
    private val docProjection = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_MIME_TYPE, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_LAST_MODIFIED, Document.COLUMN_FLAGS, Document.COLUMN_SIZE)

    override fun onCreate(): Boolean = true

    private fun prefs() = context!!.getSharedPreferences("FlutterSharedPreferences", Context.MODE_PRIVATE)

    private fun connect(path: String, query: Map<String, String> = emptyMap()): HttpURLConnection {
        var address = (prefs().getString("flutter.address", "") ?: "").trim()
        val key = (prefs().getString("flutter.key", "") ?: "").trim()
        if (address.isEmpty() || key.isEmpty()) throw IllegalStateException("The server is not set up")
        if (!address.contains("://")) address = "http://$address"
        val q = if (query.isEmpty()) "" else "?" + query.entries.joinToString("&") { "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}" }
        val c = URL(address.trimEnd('/') + path + q).openConnection() as HttpURLConnection
        c.connectTimeout = 10000
        c.readTimeout = 30000
        c.setRequestProperty("Authorization", "Bearer $key")
        return c
    }

    private fun json(path: String, query: Map<String, String> = emptyMap()): JSONObject {
        val c = connect(path, query)
        try {
            if (c.responseCode >= 400) throw IllegalStateException("HTTP ${c.responseCode}")
            return JSONObject(c.inputStream.bufferedReader(Charsets.UTF_8).readText())
        } finally {
            c.disconnect()
        }
    }

    private fun mime(name: String, dir: Boolean): String {
        if (dir) return Document.MIME_TYPE_DIR
        val ext = name.substringAfterLast('.', "").lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
    }

    private fun addRow(cursor: MatrixCursor, e: Entry) {
        known[e.id] = e
        cursor.newRow()
            .add(Document.COLUMN_DOCUMENT_ID, e.id)
            .add(Document.COLUMN_DISPLAY_NAME, e.name)
            .add(Document.COLUMN_MIME_TYPE, mime(e.name, e.dir))
            .add(Document.COLUMN_SIZE, if (e.dir) null else e.size)
            .add(Document.COLUMN_LAST_MODIFIED, e.modified)
            .add(Document.COLUMN_FLAGS, 0)
    }

    override fun queryRoots(projection: Array<out String>?): Cursor {
        val c = MatrixCursor(projection ?: rootProjection)
        val configured = (prefs().getString("flutter.address", "") ?: "").isNotBlank() && (prefs().getString("flutter.key", "") ?: "").isNotBlank()
        if (configured) {
            c.newRow()
                .add(Root.COLUMN_ROOT_ID, "basalt")
                .add(Root.COLUMN_FLAGS, 0)
                .add(Root.COLUMN_ICON, R.mipmap.ic_launcher)
                .add(Root.COLUMN_TITLE, "Basalt - PC")
                .add(Root.COLUMN_SUMMARY, "Cartelle del PC / PC folders")
                .add(Root.COLUMN_DOCUMENT_ID, ROOT_DOC)
                .add(Root.COLUMN_MIME_TYPES, "*/*")
        }
        return c
    }

    override fun queryDocument(documentId: String, projection: Array<out String>?): Cursor {
        val c = MatrixCursor(projection ?: docProjection)
        if (documentId == ROOT_DOC) {
            addRow(c, Entry(ROOT_DOC, "Basalt - PC", true, 0, 0))
        } else {
            val e = known[documentId] ?: Entry(documentId, documentId.substringAfterLast('\\').substringAfterLast('/'), false, 0, 0)
            addRow(c, e)
        }
        return c
    }

    override fun queryChildDocuments(parentDocumentId: String, projection: Array<out String>?, sortOrder: String?): Cursor {
        val c = MatrixCursor(projection ?: docProjection)
        try {
            if (parentDocumentId == ROOT_DOC) {
                val roots = json("/api/files/roots").getJSONArray("roots")
                for (i in 0 until roots.length()) {
                    val r = roots.getJSONObject(i)
                    addRow(c, Entry(r.getString("path"), r.getString("name"), true, 0, 0))
                }
            } else {
                val entries = json("/api/files/list", mapOf("path" to parentDocumentId)).getJSONArray("entries")
                for (i in 0 until entries.length()) {
                    val e = entries.getJSONObject(i)
                    addRow(c, Entry(e.getString("path"), e.getString("name"), e.getBoolean("dir"), e.optLong("size"), (e.optDouble("mtime") * 1000).toLong()))
                }
            }
        } catch (e: Exception) {
            val extras = android.os.Bundle()
            extras.putString(DocumentsContract.EXTRA_ERROR, e.message ?: "error")
            c.extras = extras
        }
        return c
    }

    override fun isChildDocument(parentDocumentId: String, documentId: String): Boolean =
        parentDocumentId == ROOT_DOC || documentId.startsWith(parentDocumentId)

    override fun openDocument(documentId: String, mode: String, signal: CancellationSignal?): ParcelFileDescriptor {
        val dir = File(context!!.cacheDir, "basalt_docs")
        dir.mkdirs()
        val name = documentId.substringAfterLast('\\').substringAfterLast('/')
        val file = File(dir, "${documentId.hashCode().toUInt()}_$name")
        val c = connect("/api/files/read", mapOf("path" to documentId))
        try {
            if (c.responseCode >= 400) throw java.io.FileNotFoundException("HTTP ${c.responseCode}")
            c.inputStream.use { input -> file.outputStream().use { out -> input.copyTo(out) } }
        } finally {
            c.disconnect()
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    }
}
