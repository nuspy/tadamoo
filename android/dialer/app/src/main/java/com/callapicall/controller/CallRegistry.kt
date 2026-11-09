package com.callapicall.controller

import android.telecom.Call
import android.telecom.VideoProfile
import android.util.Log

/**
 * Stato condiviso della chiamata corrente.
 *
 * Alimentato da [CallControllerInCallService] (l'InCallService di sistema) e
 * letto dal server HTTP. Da quando l'app e' il dialer predefinito,
 * l'InCallService riceve TUTTE le chiamate — in arrivo e in uscita — con
 * accesso pieno a stato e numero dell'interlocutore.
 */
object CallRegistry {

    /**
     * Chi sa instradare l'audio della chiamata sul Bluetooth (il PC). Lo
     * implementa [CallControllerInCallService], perche' `setAudioRoute` e'
     * un metodo dell'InCallService. Il gateway e' headless: l'audio deve
     * finire sul PC da solo, senza toccare lo schermo del telefono.
     */
    interface AudioRouter {
        fun routeToBluetooth()
    }

    @Volatile
    var audioRouter: AudioRouter? = null

    @Volatile
    private var currentCall: Call? = null

    @Volatile
    var state: String = "IDLE"
        private set

    /** Numero dell'interlocutore (chiamante in arrivo o numero composto). */
    @Volatile
    var callerNumber: String = ""
        private set

    /** "INCOMING" | "OUTGOING" | "". */
    @Volatile
    var direction: String = ""
        private set

    /** Timestamp (ms) dell'ultimo cambio di stato/numero. */
    @Volatile
    var since: Long = System.currentTimeMillis()
        private set

    private val callback = object : Call.Callback() {
        @Suppress("DEPRECATION")
        override fun onStateChanged(call: Call, newState: Int) = refresh()
        override fun onDetailsChanged(call: Call, details: Call.Details) = refresh()
    }

    @Synchronized
    fun onCallAdded(call: Call) {
        currentCall = call
        call.registerCallback(callback)
        Log.i(TAG, "chiamata aggiunta")
        refresh()
    }

    @Synchronized
    fun onCallRemoved(call: Call) {
        call.unregisterCallback(callback)
        if (currentCall === call) currentCall = null
        Log.i(TAG, "chiamata rimossa")
        refresh()
    }

    /** Risponde alla chiamata in arrivo. `false` se non c'e' una chiamata. */
    @Synchronized
    fun answer(): Boolean {
        val call = currentCall ?: return false
        call.answer(VideoProfile.STATE_AUDIO_ONLY)
        Log.i(TAG, "answer")
        return true
    }

    /** Termina la chiamata corrente. `false` se non c'e' una chiamata. */
    @Synchronized
    fun hangup(): Boolean {
        val call = currentCall ?: return false
        call.disconnect()
        Log.i(TAG, "hangup")
        return true
    }

    @Suppress("DEPRECATION")
    private fun refresh() {
        val call = currentCall
        if (call == null) {
            apply("IDLE", "", "")
            return
        }
        val details = call.details
        val number = details.handle?.schemeSpecificPart ?: ""
        val dir = when (details.callDirection) {
            Call.Details.DIRECTION_INCOMING -> "INCOMING"
            Call.Details.DIRECTION_OUTGOING -> "OUTGOING"
            else -> ""
        }
        val st = when (call.state) {
            Call.STATE_RINGING -> "RINGING"
            Call.STATE_DIALING, Call.STATE_CONNECTING -> "DIALING"
            Call.STATE_ACTIVE, Call.STATE_HOLDING -> "OFFHOOK"
            Call.STATE_DISCONNECTED, Call.STATE_DISCONNECTING -> "IDLE"
            else -> "UNKNOWN"
        }
        apply(st, number, dir)
    }

    /** true finche' la chiamata e' in corsa (per ri-forzare la route BT). */
    fun wantsBluetooth(): Boolean = state == "OFFHOOK" || state == "DIALING"

    private fun apply(newState: String, number: String, dir: String) {
        val wasRunning = state == "OFFHOOK" || state == "DIALING"
        if (newState != state || number != callerNumber) {
            since = System.currentTimeMillis()
            Log.i(TAG, "stato -> $newState ($dir $number)")
        }
        state = newState
        callerNumber = number
        direction = dir
        // Appena la chiamata parte, manda l'audio sul PC (Bluetooth HFP) da
        // solo: nessuna interazione, il telefono e' un gateway headless.
        if ((newState == "OFFHOOK" || newState == "DIALING") && !wasRunning) {
            audioRouter?.routeToBluetooth()
        }
    }

    private const val TAG = "CallRegistry"
}
