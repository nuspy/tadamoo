package com.callapicall.controller

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.IBinder
import android.util.Log
import fi.iki.elonen.NanoHTTPD

/**
 * Foreground Service che ospita il server HTTP di controllo e fa in modo che
 * il call-bridge riesca a trovarlo da solo.
 *
 * Lo stato delle chiamate e' gestito separatamente dal [CallRegistry], che
 * viene alimentato dall'InCallService di sistema; questo servizio si limita
 * a tenere vivo il server HTTP.
 *
 * Perche' la discovery: l'indirizzo IP del telefono cambia a ogni
 * riconnessione al Wi-Fi, quindi configurarlo a mano sul PC e' una battaglia
 * persa. Il servizio si annuncia su due canali indipendenti — mDNS/NSD e una
 * risposta a probe UDP — perche' nessuno dei due funziona su tutte le reti.
 */
class ControlService : Service() {

    private var server: HttpServer? = null
    private var responder: DiscoveryResponder? = null
    private var nsdManager: NsdManager? = null
    private var nsdListener: NsdManager.RegistrationListener? = null
    private var multicastLock: WifiManager.MulticastLock? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIF_ID, buildNotification())

        if (server == null) {
            val token = Pairing.token(this)
            if (token.isEmpty()) {
                // Senza token il server accetterebbe chiunque sulla rete: non parte.
                Log.w(TAG, "Nessun token inserito: server HTTP non avviato")
                stopSelf()
                return START_NOT_STICKY
            }
            val controller = CallController(this)
            val srv = HttpServer(PORT, token, controller, this)
            try {
                srv.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
                server = srv
                Log.i(TAG, "Server HTTP avviato sulla porta $PORT")
                startDiscovery()
            } catch (e: Exception) {
                Log.e(TAG, "Avvio del server HTTP fallito", e)
                stopSelf()
            }
        }
        // START_STICKY: il sistema riavvia il servizio se viene terminato.
        return START_STICKY
    }

    override fun onDestroy() {
        stopDiscovery()
        server?.stop()
        server = null
        Log.i(TAG, "ControlService terminato")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // --- Discovery --------------------------------------------------------

    private fun startDiscovery() {
        acquireMulticastLock()
        responder = DiscoveryResponder(this, PORT).also { it.start() }
        registerNsd()
    }

    /**
     * Senza questo lock il Wi-Fi scarta i pacchetti multicast destinati ad
     * altri: l'annuncio mDNS risulta registrato lato telefono, ma le query del
     * PC non arrivano mai e il servizio resta invisibile. Costa un po' di
     * batteria, accettabile per un telefono che fa da gateway fisso.
     */
    private fun acquireMulticastLock() {
        try {
            val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            multicastLock = wifi.createMulticastLock("callapicall-mdns").apply {
                setReferenceCounted(false)
                acquire()
            }
            Log.i(TAG, "MulticastLock acquisito: le query mDNS possono arrivare")
        } catch (e: Exception) {
            Log.w(TAG, "MulticastLock non acquisito: mDNS potrebbe non rispondere", e)
        }
    }

    private fun stopDiscovery() {
        responder?.stop()
        responder = null
        try {
            multicastLock?.takeIf { it.isHeld }?.release()
        } catch (e: Exception) {
            Log.w(TAG, "rilascio del MulticastLock fallito", e)
        }
        multicastLock = null
        val manager = nsdManager
        val listener = nsdListener
        if (manager != null && listener != null) {
            try {
                manager.unregisterService(listener)
            } catch (e: Exception) {
                Log.w(TAG, "deregistrazione mDNS fallita", e)
            }
        }
        nsdManager = null
        nsdListener = null
    }

    /**
     * Annuncia il servizio via mDNS. Nei record TXT finisce solo la scheda
     * pubblica (id, modello): viaggiano in chiaro e sono visibili a chiunque
     * sia sulla rete, mentre il nome Bluetooth sta dietro /identify.
     */
    private fun registerNsd() {
        val info = NsdServiceInfo().apply {
            // Il nome deve essere unico sulla rete: due telefoni con l'app
            // attiva non devono darsi fastidio.
            serviceName = "$SERVICE_NAME-${DeviceIdentity.id(this@ControlService).take(6)}"
            serviceType = SERVICE_TYPE
            port = PORT
            val card = DeviceIdentity.publicCard(this@ControlService, PORT)
            setAttribute("id", card.optString("id"))
            setAttribute("model", card.optString("model"))
            setAttribute("api", DeviceIdentity.API_VERSION.toString())
        }

        val listener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) {
                Log.i(TAG, "annuncio mDNS attivo: ${info.serviceName}")
            }

            override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) {
                Log.w(TAG, "annuncio mDNS fallito (codice $errorCode)")
            }

            override fun onServiceUnregistered(info: NsdServiceInfo) {
                Log.i(TAG, "annuncio mDNS ritirato")
            }

            override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) {
                Log.w(TAG, "ritiro dell'annuncio mDNS fallito (codice $errorCode)")
            }
        }

        try {
            val manager = getSystemService(Context.NSD_SERVICE) as NsdManager
            manager.registerService(info, NsdManager.PROTOCOL_DNS_SD, listener)
            nsdManager = manager
            nsdListener = listener
        } catch (e: Exception) {
            // Senza mDNS restano il probe UDP e, sul PC, la scansione della rete.
            Log.w(TAG, "registrazione mDNS non riuscita", e)
        }
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Call API Controller",
            NotificationManager.IMPORTANCE_LOW,
        )
        nm.createNotificationChannel(channel)

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Call API Controller attivo")
            .setContentText("In ascolto sulla porta $PORT")
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "ControlService"
        private const val CHANNEL_ID = "control_service"
        private const val NOTIF_ID = 1

        /** Porta del server HTTP di controllo. */
        const val PORT = 8080

        /** Tipo di servizio mDNS con cui il call-bridge cerca i telefoni. */
        const val SERVICE_TYPE = "_callapicall._tcp"
        const val SERVICE_NAME = "callapicall"
    }
}
