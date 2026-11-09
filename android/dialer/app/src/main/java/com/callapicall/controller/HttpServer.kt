package com.callapicall.controller

import android.content.Context
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import org.json.JSONObject

/**
 * Server HTTP di controllo. Espone una piccola REST API consumata dal
 * call-bridge sul PC. Tutte le risposte sono JSON.
 *
 * Endpoint:
 *   GET  /health   -> {"status":"ok"}
 *   GET  /identify -> carta d'identita' del telefono (modello, nome BT, id)
 *   GET  /status   -> {"state":...,"number":...,"direction":...,"since":<ms>}
 *   POST /call     body {"number":"+39..."} -> avvia chiamata (TelecomManager)
 *   POST /answer   -> risponde alla chiamata in arrivo (CallRegistry)
 *   POST /hangup   -> termina la chiamata corrente (CallRegistry)
 *
 * Auth: header "Authorization: Bearer <token>", sempre richiesta.
 */
class HttpServer(
    port: Int,
    private val authToken: String,
    private val callController: CallController,
    private val context: Context,
) : NanoHTTPD(port) {

    override fun serve(session: IHTTPSession): Response {
        return try {
            val auth = session.headers["authorization"] ?: ""
            if (authToken.isEmpty() || auth != "Bearer $authToken") {
                return json(Response.Status.UNAUTHORIZED, mapOf("error" to "unauthorized"))
            }

            // NanoHTTPD richiede di consumare il body delle richieste con payload.
            var body = "{}"
            if (session.method == Method.POST || session.method == Method.PUT) {
                val files = HashMap<String, String>()
                session.parseBody(files)
                body = files["postData"] ?: "{}"
            }

            when (session.uri.trimEnd('/').ifEmpty { "/" }) {
                "/health" -> json(Response.Status.OK, mapOf("status" to "ok"))
                "/identify" -> jsonObject(
                    Response.Status.OK,
                    DeviceIdentity.full(context, listeningPort),
                )
                "/status" -> handleStatus()
                "/call" -> handleCall(session, body)
                "/answer" -> requirePost(session) {
                    resultResponse(CallRegistry.answer(), "nessuna chiamata da rispondere")
                }
                "/hangup" -> requirePost(session) {
                    resultResponse(CallRegistry.hangup(), "nessuna chiamata da terminare")
                }
                else -> json(Response.Status.NOT_FOUND, mapOf("error" to "not found"))
            }
        } catch (e: Exception) {
            Log.e(TAG, "serve() errore", e)
            json(Response.Status.INTERNAL_ERROR, mapOf("error" to (e.message ?: "error")))
        }
    }

    private fun handleStatus(): Response = json(
        Response.Status.OK,
        mapOf(
            "state" to CallRegistry.state,
            "number" to CallRegistry.callerNumber,
            "direction" to CallRegistry.direction,
            "since" to CallRegistry.since,
        ),
    )

    private fun handleCall(session: IHTTPSession, body: String): Response {
        if (session.method != Method.POST) {
            return json(Response.Status.METHOD_NOT_ALLOWED, mapOf("error" to "usa POST"))
        }
        val number = JSONObject(body).optString("number", "").trim()
        if (number.isEmpty()) {
            return json(Response.Status.BAD_REQUEST, mapOf("error" to "campo 'number' mancante"))
        }
        val result = callController.placeCall(number)
        return if (result.isSuccess) {
            json(Response.Status.OK, mapOf("ok" to true, "number" to number))
        } else {
            json(
                Response.Status.INTERNAL_ERROR,
                mapOf("error" to (result.exceptionOrNull()?.message ?: "placeCall fallita")),
            )
        }
    }

    private fun requirePost(session: IHTTPSession, action: () -> Response): Response =
        if (session.method == Method.POST) action()
        else json(Response.Status.METHOD_NOT_ALLOWED, mapOf("error" to "usa POST"))

    private fun resultResponse(ok: Boolean, errorMsg: String): Response =
        if (ok) {
            json(Response.Status.OK, mapOf("ok" to true))
        } else {
            json(Response.Status.BAD_REQUEST, mapOf("error" to errorMsg))
        }

    private fun json(status: Response.Status, data: Map<String, Any?>): Response {
        val obj = JSONObject()
        for ((key, value) in data) obj.put(key, value)
        return jsonObject(status, obj)
    }

    private fun jsonObject(status: Response.Status, obj: JSONObject): Response =
        newFixedLengthResponse(status, "application/json", obj.toString())

    companion object {
        private const val TAG = "HttpServer"
    }
}
