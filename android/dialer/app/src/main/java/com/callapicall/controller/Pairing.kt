package com.callapicall.controller

import android.content.Context

/**
 * Il token con cui il PC si presenta a questo telefono. Lo scrive la persona
 * nella schermata principale, la stessa stringa che il servizio vocale ha nella
 * sua configurazione; non e' mai nel codice. Senza token il server HTTP non parte.
 */
object Pairing {
    private const val FILE = "pairing"
    private const val KEY = "token"

    fun token(context: Context): String =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(KEY, "")?.trim() ?: ""

    fun save(context: Context, token: String) {
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putString(KEY, token.trim()).apply()
    }
}
