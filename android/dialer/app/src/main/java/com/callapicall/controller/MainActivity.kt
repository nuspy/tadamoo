package com.callapicall.controller

import android.Manifest
import android.app.role.RoleManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * UI minima: concede i permessi, imposta l'app come telefono predefinito,
 * avvia/ferma il [ControlService] e mostra l'indirizzo per il call-bridge.
 *
 * Gli intent-filter DIAL nel manifest rendono questa Activity la "dialer UI"
 * minima richiesta per essere eleggibili come app telefono predefinita.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var statusView: TextView
    private lateinit var tokenField: EditText

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { updateStatus() }

    private val roleLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { updateStatus() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        statusView = TextView(this)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
            addView(statusView)
            tokenField = EditText(this@MainActivity).apply {
                hint = "Token di accoppiamento"
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                setSingleLine()
            }
            addView(tokenField)
            addView(button("Salva token") { saveToken() })
            addView(button("Concedi permessi") { requestPermissions() })
            addView(button("Imposta come telefono predefinito") { requestDialerRole() })
            addView(button("Avvia servizio") { startControlService(); updateStatus() })
            addView(button("Ferma servizio") {
                stopService(Intent(this@MainActivity, ControlService::class.java))
                updateStatus()
            })
        }
        setContentView(root)

        requestPermissions()
    }

    override fun onResume() {
        super.onResume()
        // Aprire l'app basta a rimettere in piedi il gateway: il telefono puo'
        // riavviarsi (o essere terminato dal sistema) e nessuno e' li' a
        // premere "Avvia servizio". ControlService.onStartCommand e'
        // idempotente, quindi chiamarlo a ogni resume non fa danni.
        if (hasRequiredPermissions()) startControlService()
        updateStatus()
    }

    private fun button(label: String, onClick: () -> Unit): Button =
        Button(this).apply {
            text = label
            setOnClickListener { onClick() }
        }

    private fun requestPermissions() {
        val perms = mutableListOf(
            Manifest.permission.CALL_PHONE,
            Manifest.permission.READ_PHONE_STATE,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        permissionLauncher.launch(perms.toTypedArray())
    }

    /** Chiede di diventare l'app telefono predefinita (ruolo DIALER). */
    private fun requestDialerRole() {
        val roleManager = getSystemService(RoleManager::class.java) ?: return
        if (!roleManager.isRoleAvailable(RoleManager.ROLE_DIALER)) return
        if (roleManager.isRoleHeld(RoleManager.ROLE_DIALER)) return
        roleLauncher.launch(roleManager.createRequestRoleIntent(RoleManager.ROLE_DIALER))
    }

    private fun isDefaultDialer(): Boolean {
        val roleManager = getSystemService(RoleManager::class.java) ?: return false
        return roleManager.isRoleHeld(RoleManager.ROLE_DIALER)
    }

    /** Salva il token e riavvia il servizio, che lo legge solo quando parte. */
    private fun saveToken() {
        Pairing.save(this, tokenField.text.toString())
        tokenField.setText("")
        stopService(Intent(this, ControlService::class.java))
        if (hasRequiredPermissions()) startControlService()
        updateStatus()
    }

    private fun startControlService() {
        startForegroundService(Intent(this, ControlService::class.java))
    }

    /** I permessi senza i quali il servizio non potrebbe fare il suo lavoro. */
    private fun hasRequiredPermissions(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CALL_PHONE) ==
            PackageManager.PERMISSION_GRANTED

    private fun updateStatus() {
        val ip = localIpAddress()
        val dialer = if (isDefaultDialer()) "SI" else "NO — premi il pulsante sopra"
        statusView.text = buildString {
            appendLine("Telefono predefinito: $dialer")
            appendLine("Token: " + if (Pairing.token(this).isEmpty()) "NON INSERITO — il servizio non parte" else "inserito")
            appendLine()
            appendLine("Indirizzo per il call-bridge sul PC:")
            appendLine("  http://$ip:${ControlService.PORT}")
            appendLine()
            appendLine("Endpoint:")
            appendLine("  GET  /health")
            appendLine("  GET  /status")
            appendLine("  POST /call    {\"number\":\"+39...\"}")
            appendLine("  POST /answer")
            appendLine("  POST /hangup")
            appendLine()
            appendLine("Auth: header  Authorization: Bearer <token>")
        }
    }

    /** Primo indirizzo IPv4 non-loopback (tipicamente quello Wi-Fi). */
    private fun localIpAddress(): String {
        try {
            for (intf in NetworkInterface.getNetworkInterfaces()) {
                for (addr in intf.inetAddresses) {
                    if (!addr.isLoopbackAddress && addr is Inet4Address) {
                        return addr.hostAddress ?: continue
                    }
                }
            }
        } catch (_: Exception) {
            // ignorato: sotto si ritorna un placeholder
        }
        return "<ip-del-telefono>"
    }
}
