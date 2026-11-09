#pragma once

#include <string>

#include "lvgl.h"
#include "esp_brookesia.hpp"
#include "AvatarFace.hpp"

/**
 * Terzo terminale di callAPIcall, accanto al PC e all'app Android.
 *
 * Qui gira solo il CLIENT: STT, LLM, TTS e la generazione del labiale restano
 * sul PC, come nell'app Android. Il device mostra la persona, prende la voce e
 * la rimanda — niente di piu'.
 *
 * Il protocollo e' quello che il gateway parla gia': JSON e PCM16 grezzo su un
 * WebSocket (porta 8771), piu' il video H.264 su WebRTC quando serve. Vedi
 * `esp32p4.md` nella radice del repo per i vincoli verificati del protocollo, e
 * `components/gateway_client` per il trasporto.
 *
 * Stato dell'audio: la board non ha ancora microfono e altoparlante. Il tasto
 * manda quindi un **turno scritto** (`{"type":"text"}`, che il gateway accetta),
 * cosi' la catena device -> agente -> risposta si prova per intero; quando
 * l'hardware audio arrivera', al suo posto andranno i frame PCM del microfono.
 *
 * Il volto: se il renderer (AvatarFace) supera il proprio controllo l'app
 * dichiara `visemes` nell'auth e disegna una bocca che segue la timeline del PC;
 * altrimenti dichiara niente e resta la sola voce.
 */
class AppAvatar: public ESP_Brookesia_PhoneApp
{
public:
    AppAvatar();
    ~AppAvatar();

    bool run(void);
    bool back(void);
    bool close(void);

    /** Stato mostrato all'utente, in un posto solo per non sparpagliare i testi. */
    enum class Stato {
        SCOLLEGATO,
        COLLEGAMENTO,
        PRONTO,
        ASCOLTO,
        PENSA,
        PARLA,
        ERRORE,
    };

private:
    void costruisciInterfaccia(void);
    void mostraStato(Stato stato, const char *dettaglio = nullptr);
    void mostraTesto(const char *testo);
    void avviaTurno(void);
    /** Il PC ha scelto un modo di disegno (ready o render_mode). */
    void applicaModo(const char *modo);

    static void ptt_event_cb(lv_event_t *e);
    /** Riceve gli eventi del gateway: gira nel task del WebSocket. */
    static void gateway_cb(const void *event, void *user_ctx);

    Stato _stato = Stato::SCOLLEGATO;

    // Il palco della persona: per ora il poster, poi il video decodificato.
    lv_obj_t *_palco = nullptr;
    lv_obj_t *_palco_testo = nullptr;
    // Pannello di destra.
    lv_obj_t *_spia = nullptr;          // pallino colorato dello stato
    lv_obj_t *_stato_label = nullptr;
    lv_obj_t *_ptt = nullptr;           // premi per parlare (o per il turno scritto)
    lv_obj_t *_trascrizione = nullptr;
    lv_obj_t *_ptt_testo = nullptr;

    uint16_t _width = 0;
    uint16_t _height = 0;
    int _palco_w = 0;
    int _palco_h = 0;

    AvatarFace _face;
    // Ultimo modo che il PC ha scelto e che non si sa disegnare: gia' segnalato.
    char _modo_rifiutato[16] = "";

    // La risposta arriva a pezzi: si accumula qui per mostrarla mentre cresce.
    std::string _risposta;
};
