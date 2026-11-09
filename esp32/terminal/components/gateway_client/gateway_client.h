/*
 * Client del voice gateway di callAPIcall (WebSocket su :8771).
 *
 * Il device è un terminale come l'app Android: qui non c'è né STT né TTS né
 * l'agente, solo il trasporto. Il protocollo è quello che il gateway parla già —
 * JSON di testo più frame binari PCM16 LE mono — ed è documentato in
 * `call-bridge/callbridge/voice_gateway.py`.
 *
 * Giro di un turno:
 *   ->  {"type":"auth","token":"..."}        entro 10 s dall'apertura
 *   <-  {"type":"ready"}
 *   ->  {"type":"start","mode":"hold","lang":"it","sample_rate":16000}
 *   ->  frame binari PCM16                   (oppure {"type":"text"}, vedi sotto)
 *   ->  {"type":"end"}                       (modalità "hold": pulsante rilasciato)
 *   <-  {"type":"transcript","text":"..."}   cosa ha sentito
 *   <-  {"type":"reply","chunk":"..."}       risposta dell'agente, a pezzi
 *   <-  {"type":"audio_start","sample_rate":24000} + frame binari + audio_end
 *   <-  {"type":"done"}
 *
 * Finché sulla board non ci sono microfono e altoparlante, si usa
 * `gateway_send_text()`: il gateway accetta un turno scritto al posto
 * dell'audio, quindi la catena device -> agente -> risposta si prova per intero
 * senza hardware audio.
 *
 * Tre vincoli del gateway che un client sbaglia al primo tentativo (verificati
 * nel suo codice): l'`auth` deve arrivare entro 10 s, i frame binari mandati
 * fuori turno vengono scartati in silenzio, e la lunghezza di ogni frame audio
 * deve essere **pari** — un frame dispari fa cadere la connessione.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    GATEWAY_EV_CONNECTED,      /**< WebSocket aperto, auth inviata */
    GATEWAY_EV_READY,          /**< il gateway è pronto a ricevere turni */
    GATEWAY_EV_TRANSCRIPT,     /**< `text`: cosa ha capito di noi */
    GATEWAY_EV_REPLY,          /**< `text`: un pezzo della risposta */
    GATEWAY_EV_AUDIO_START,    /**< `sample_rate` dell'audio che segue */
    GATEWAY_EV_AUDIO_DATA,     /**< `audio`/`audio_len`: PCM16 della risposta */
    GATEWAY_EV_AUDIO_END,
    GATEWAY_EV_DONE,           /**< turno concluso */
    GATEWAY_EV_ERROR,          /**< `text`: messaggio del gateway */
    GATEWAY_EV_DISCONNECTED,
} gateway_event_type_t;

typedef struct {
    gateway_event_type_t type;
    const char *text;          /**< valorizzato per TRANSCRIPT, REPLY, ERROR */
    int sample_rate;           /**< valorizzato per AUDIO_START */
    const void *audio;         /**< valorizzato per AUDIO_DATA */
    size_t audio_len;
} gateway_event_t;

typedef void (*gateway_event_cb_t)(const gateway_event_t *event, void *user_ctx);

/**
 * Apre la connessione e resta in piedi da sola: il client di ESP-IDF riconnette
 * quando la rete cade, e a ogni riapertura l'`auth` viene rimandata.
 *
 * `uri` è per esempio "ws://192.168.1.10:8771". `token` può essere vuoto se il
 * gateway gira senza token (il messaggio di auth va mandato comunque).
 */
esp_err_t gateway_client_start(const char *uri, const char *token,
                               gateway_event_cb_t cb, void *user_ctx);

esp_err_t gateway_client_stop(void);

/** True fra `ready` e la caduta della connessione. */
bool gateway_client_is_ready(void);

/** Apre un turno. `mode` è "hold" (pulsante) o "continuous" (VAD lato PC). */
esp_err_t gateway_turn_start(const char *mode, const char *lang,
                             const char *voice, int sample_rate);

/** Un frame di microfono. `len` deve essere pari: sono campioni int16. */
esp_err_t gateway_send_audio(const void *pcm, size_t len);

/** Chiude il turno in modalità "hold". */
esp_err_t gateway_turn_end(void);

/**
 * Turno scritto invece che parlato: è la via per provare tutta la catena
 * mentre la board non ha ancora microfono e altoparlante.
 */
esp_err_t gateway_send_text(const char *text);

#ifdef __cplusplus
}
#endif
