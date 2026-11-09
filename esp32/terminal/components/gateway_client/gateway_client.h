/*
 * Client del voice gateway (WebSocket).
 *
 * Il device è un terminale come l'app Android: qui non c'è né STT né TTS né
 * l'agente, solo il trasporto. Il protocollo è quello che il gateway parla già —
 * JSON di testo più frame binari PCM16 LE mono — ed è documentato in
 * `docs/voice/gateway.md` (Render modes) e `docs/voice/esp32.md`.
 *
 * Giro di un turno:
 *   ->  {"type":"auth","token":"...","render_modes":[...],"device":"esp32"}
 *                                            entro 10 s dall'apertura
 *   <-  {"type":"ready","render_mode":"visemes"|"none",...}
 *   ->  {"type":"start","mode":"hold","lang":"it","sample_rate":16000}
 *   ->  frame binari PCM16                   (oppure {"type":"text"}, vedi sotto)
 *   ->  {"type":"end"}                       (modalità "hold": pulsante rilasciato)
 *   <-  {"type":"transcript","text":"..."}   cosa ha sentito
 *   <-  {"type":"reply","chunk":"..."}       risposta dell'agente, a pezzi
 *   <-  {"type":"audio_start","sample_rate":24000,"segment":n}
 *   <-  {"type":"visemes",...} prima dei frame binari di ogni segmento (se "visemes")
 *   <-  frame binari + audio_end
 *   <-  {"type":"done"}
 *
 * `render_modes` dichiara solo cio' che la board sa disegnare davvero: va
 * impostato con `gateway_client_set_render_modes()` prima di `start`. Senza
 * dichiarazione il gateway manda la sola voce. I messaggi che il client non
 * conosce (video_*, ecc.) sono ignorati.
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
#include "viseme_timeline.h"

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
    GATEWAY_EV_VISEMES,        /**< `visemes`: la timeline del prossimo segmento audio */
    GATEWAY_EV_INTERRUPT,      /**< il PC ha fermato la risposta */
    GATEWAY_EV_RENDER_MODE,    /**< `mode`: il modo di disegno scelto dal PC */
} gateway_event_type_t;

typedef struct {
    gateway_event_type_t type;
    const char *text;          /**< valorizzato per TRANSCRIPT, REPLY, ERROR */
    int sample_rate;           /**< valorizzato per AUDIO_START */
    const void *audio;         /**< valorizzato per AUDIO_DATA */
    size_t audio_len;
    const char *mode;          /**< READY e RENDER_MODE: "visemes", "none", ... ("none" se manca) */
    int segment;               /**< AUDIO_START: numero del segmento (0 se manca) */
    const vt_segment_t *visemes; /**< VISEMES: valido solo dentro la callback */
} gateway_event_t;

typedef void (*gateway_event_cb_t)(const gateway_event_t *event, void *user_ctx);

/**
 * Apre la connessione e resta in piedi da sola: il client di ESP-IDF riconnette
 * quando la rete cade, e a ogni riapertura l'`auth` viene rimandata.
 *
 * `uri` è per esempio "ws://192.168.1.10:8771". `token` può essere vuoto se il
 * gateway gira senza token (il messaggio di auth va mandato comunque).
 */
/**
 * I modi di disegno dichiarati nell'`auth`, dal migliore: oggi solo "visemes",
 * e solo se il renderer ha superato il proprio controllo. Nomi [a-z0-9_-], fino
 * a 15 caratteri, al massimo 4. Va chiamata prima di `gateway_client_start`;
 * `count` 0 dichiara niente (voce soltanto). Cambiare la dichiarazione vuol
 * dire riaprire la connessione.
 */
esp_err_t gateway_client_set_render_modes(const char *const *modes, size_t count);

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

/** Ferma la risposta in corso (un tocco mentre parla). */
esp_err_t gateway_send_interrupt(void);

/** Dice al PC che un modo dichiarato non si puo' disegnare: passa al successivo. */
esp_err_t gateway_send_render_failed(const char *mode, const char *why);

#ifdef __cplusplus
}
#endif
