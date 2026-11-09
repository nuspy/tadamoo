package com.callapicall.controller

import android.bluetooth.BluetoothDevice
import android.os.Build
import android.telecom.Call
import android.telecom.CallAudioState
import android.telecom.InCallService
import android.util.Log

/**
 * InCallService: il sistema vi associa le chiamate quando l'app e' il dialer
 * predefinito. Inoltra le chiamate al [CallRegistry] e non mostra alcuna UI
 * (il telefono e' un gateway headless: l'audio va in Bluetooth al PC, il
 * controllo arriva via HTTP).
 *
 * Instrada l'audio della chiamata sul Bluetooth (il PC) automaticamente, senza
 * interazione: e' il requisito del gateway, che deve telefonare in assenza
 * dell'utente. `setAudioRoute`/`requestBluetoothAudio` sono metodi
 * dell'InCallService, quindi la logica di routing vive qui.
 */
class CallControllerInCallService : InCallService(), CallRegistry.AudioRouter {

    override fun onCreate() {
        super.onCreate()
        CallRegistry.audioRouter = this
    }

    override fun onDestroy() {
        if (CallRegistry.audioRouter === this) CallRegistry.audioRouter = null
        super.onDestroy()
    }

    override fun onCallAdded(call: Call) {
        super.onCallAdded(call)
        Log.i(TAG, "onCallAdded")
        CallRegistry.onCallAdded(call)
    }

    override fun onCallRemoved(call: Call) {
        super.onCallRemoved(call)
        Log.i(TAG, "onCallRemoved")
        CallRegistry.onCallRemoved(call)
    }

    /**
     * Il route audio disponibile cambia poco dopo l'inizio della chiamata (il
     * canale SCO Bluetooth non e' pronto all'istante). Qui, appena il Bluetooth
     * diventa selezionabile, lo si forza — cosi' il primo tentativo in
     * [routeToBluetooth] che arriva "troppo presto" viene comunque recuperato.
     */
    override fun onCallAudioStateChanged(audioState: CallAudioState) {
        super.onCallAudioStateChanged(audioState)
        Log.i(TAG, "audio route=${audioState.route} mask=${audioState.supportedRouteMask}")
        if (CallRegistry.wantsBluetooth() &&
            audioState.route != CallAudioState.ROUTE_BLUETOOTH &&
            (audioState.supportedRouteMask and CallAudioState.ROUTE_BLUETOOTH) != 0
        ) {
            routeToBluetooth()
        }
    }

    override fun routeToBluetooth() {
        val audioState = callAudioState
        if (audioState == null) {
            Log.w(TAG, "routeToBluetooth: nessun CallAudioState (chiamata non attiva?)")
            return
        }
        if ((audioState.supportedRouteMask and CallAudioState.ROUTE_BLUETOOTH) == 0) {
            Log.w(TAG, "routeToBluetooth: Bluetooth non tra le route " +
                "(PC non connesso come dispositivo di chiamata / 'Calls' OFF?)")
            return
        }
        // Se si conoscono i device BT, scegli quello del PC per nome (evita di
        // instradare su eventuali auricolari connessi). Altrimenti route BT
        // generica.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val pc = pickPcDevice(audioState.supportedBluetoothDevices)
            if (pc != null) {
                requestBluetoothAudio(pc)
                Log.i(TAG, "requestBluetoothAudio -> ${safeName(pc)}")
                return
            }
        }
        setAudioRoute(CallAudioState.ROUTE_BLUETOOTH)
        Log.i(TAG, "setAudioRoute -> BLUETOOTH")
    }

    private fun pickPcDevice(devices: Collection<BluetoothDevice>?): BluetoothDevice? {
        if (devices.isNullOrEmpty()) return null
        // Preferisci un device il cui nome sembra un PC; se i nomi non sono
        // leggibili (permesso) e ce n'e' uno solo, usa quello.
        devices.firstOrNull { d ->
            val n = safeName(d).lowercase()
            "pluto" in n || "pc" in n || "desktop" in n || "windows" in n
        }?.let { return it }
        return if (devices.size == 1) devices.first() else null
    }

    private fun safeName(d: BluetoothDevice): String = try {
        d.name ?: ""
    } catch (_: SecurityException) {
        ""
    }

    companion object {
        private const val TAG = "InCallService"
    }
}
