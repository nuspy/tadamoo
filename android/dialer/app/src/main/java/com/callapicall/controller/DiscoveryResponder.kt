package com.callapicall.controller

import android.content.Context
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import kotlin.concurrent.thread

/**
 * Risponde ai probe UDP in broadcast del call-bridge.
 *
 * E' la rete di sicurezza sotto mDNS: molti access point domestici filtrano il
 * multicast (o lo spengono nel risparmio energetico) e l'annuncio NSD non
 * arriva, mentre un broadcast diretto passa quasi sempre.
 *
 * Protocollo, volutamente banale: il PC manda il testo [PROBE] in broadcast
 * sulla porta [PORT]; ogni telefono con l'app attiva risponde al mittente con
 * la propria scheda pubblica in JSON. Chi risponde dice solo chi e' e su quale
 * porta ascolta: i dati veri restano dietro l'endpoint autenticato /identify.
 */
class DiscoveryResponder(private val context: Context, private val httpPort: Int) {

    @Volatile private var running = false
    private var socket: DatagramSocket? = null

    fun start() {
        if (running) return
        running = true
        thread(name = "discovery-responder", isDaemon = true) { loop() }
    }

    fun stop() {
        running = false
        // Chiudere il socket sblocca la receive() in attesa.
        socket?.close()
        socket = null
    }

    private fun loop() {
        try {
            val sock = DatagramSocket(null).apply {
                reuseAddress = true
                broadcast = true
                bind(java.net.InetSocketAddress(PORT))
            }
            socket = sock
            Log.i(TAG, "in ascolto per i probe UDP sulla porta $PORT")

            val buffer = ByteArray(256)
            while (running) {
                val packet = DatagramPacket(buffer, buffer.size)
                sock.receive(packet)
                val text = String(packet.data, 0, packet.length).trim()
                if (text != PROBE) continue
                reply(sock, packet.address, packet.port)
            }
        } catch (e: Exception) {
            // La chiusura del socket in stop() finisce qui: non e' un errore.
            if (running) Log.e(TAG, "responder UDP interrotto", e)
        }
    }

    private fun reply(sock: DatagramSocket, address: InetAddress, port: Int) {
        try {
            val payload = DeviceIdentity.publicCard(context, httpPort)
                .toString().toByteArray()
            sock.send(DatagramPacket(payload, payload.size, address, port))
            Log.i(TAG, "risposto al probe di ${address.hostAddress}")
        } catch (e: Exception) {
            Log.w(TAG, "risposta al probe fallita", e)
        }
    }

    companion object {
        private const val TAG = "DiscoveryResponder"

        /** Porta dei probe di discovery, accanto a quella del server HTTP. */
        const val PORT = 8079

        /** Testo esatto che il call-bridge manda in broadcast. */
        const val PROBE = "CALLAPICALL-DISCOVERY?"
    }
}
