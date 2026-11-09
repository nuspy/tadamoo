package com.callapicall.controller

import android.bluetooth.BluetoothManager
import android.content.Context
import android.os.Build
import android.provider.Settings
import android.util.Log
import org.json.JSONObject

/**
 * Carta d'identita' del telefono, letta dal call-bridge dopo averlo trovato
 * sulla rete.
 *
 * Il campo che conta davvero e' [btName]: su Windows gli endpoint audio del
 * profilo vivavoce si chiamano "<nome Bluetooth> Hands-Free HF Audio", quindi
 * e' la chiave che lega questo telefono al suo endpoint HFP. Senza, il PC
 * dovrebbe indovinare il modello da una lista cablata — che e' esattamente
 * cio' che rende fragile l'aggiunta di un telefono nuovo.
 */
object DeviceIdentity {

    /** Identificatore stabile del telefono (sopravvive ai cambi di IP). */
    fun id(context: Context): String =
        try {
            Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
                ?: "sconosciuto"
        } catch (e: Exception) {
            Log.w(TAG, "ANDROID_ID non leggibile", e)
            "sconosciuto"
        }

    /**
     * Nome Bluetooth del telefono (es. "Andrea's S21 Ultra").
     *
     * Su Android 12+ l'adapter richiede BLUETOOTH_CONNECT; se il permesso non
     * e' ancora stato concesso ripieghiamo su Settings.Secure, che espone lo
     * stesso nome senza permessi.
     */
    fun btName(context: Context): String {
        try {
            val manager = context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
            val name = manager?.adapter?.name
            if (!name.isNullOrBlank()) return name
        } catch (e: SecurityException) {
            Log.i(TAG, "BLUETOOTH_CONNECT non concesso, uso Settings.Secure")
        } catch (e: Exception) {
            Log.w(TAG, "nome Bluetooth non leggibile dall'adapter", e)
        }
        return try {
            Settings.Secure.getString(context.contentResolver, "bluetooth_name") ?: ""
        } catch (e: Exception) {
            Log.w(TAG, "nome Bluetooth non leggibile", e)
            ""
        }
    }

    /** Modello commerciale, es. "SM-G998B". Non contiene dati personali. */
    fun model(): String = Build.MODEL ?: "sconosciuto"

    /**
     * Scheda completa, servita da GET /identify (endpoint autenticato).
     *
     * Il nome Bluetooth sta solo qui e non nell'annuncio mDNS / nella risposta
     * UDP, che viaggiano in chiaro su tutta la rete: spesso contiene il nome
     * proprio del proprietario.
     */
    fun full(context: Context, port: Int): JSONObject = JSONObject().apply {
        put("id", id(context))
        put("model", model())
        put("manufacturer", Build.MANUFACTURER ?: "")
        put("device", Build.DEVICE ?: "")
        put("android", Build.VERSION.RELEASE ?: "")
        put("btname", btName(context))
        put("port", port)
        put("api", API_VERSION)
    }

    /**
     * Scheda ridotta per gli annunci in chiaro (mDNS TXT, risposta UDP):
     * quanto basta per distinguere due telefoni, nulla di personale.
     */
    fun publicCard(context: Context, port: Int): JSONObject = JSONObject().apply {
        put("id", id(context))
        put("model", model())
        put("port", port)
        put("api", API_VERSION)
    }

    /** Versione dell'API di controllo, per far convivere app e bridge disallineati. */
    const val API_VERSION = 1

    private const val TAG = "DeviceIdentity"
}
