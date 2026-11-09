package com.callapicall.controller

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.telecom.TelecomManager
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Avvio di chiamate in uscita via [TelecomManager].
 *
 * Risposta e riaggancio NON sono qui: passano dal [CallRegistry], perche'
 * l'app e' il dialer predefinito e l'InCallService espone gli oggetti `Call`.
 * Una volta composta, anche la chiamata in uscita arriva all'InCallService.
 */
class CallController(private val context: Context) {

    private val telecomManager =
        context.getSystemService(Context.TELECOM_SERVICE) as TelecomManager

    /** Avvia una chiamata cellulare verso [number] (es. "+39333..."). */
    fun placeCall(number: String): Result<Unit> {
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.CALL_PHONE,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            return Result.failure(SecurityException("Permesso CALL_PHONE non concesso"))
        }
        return try {
            val uri = Uri.fromParts("tel", number, null)
            telecomManager.placeCall(uri, null)
            Log.i(TAG, "placeCall -> $number")
            Result.success(Unit)
        } catch (e: Exception) {
            Log.e(TAG, "placeCall fallita", e)
            Result.failure(e)
        }
    }

    companion object {
        private const val TAG = "CallController"
    }
}
