#include "AppAvatar.hpp"

#include "esp_log.h"
#include "esp_lv_adapter.h"
#include "gateway_client.h"
#include "sdkconfig.h"

LV_IMG_DECLARE(img_app_avatar);

static const char *TAG = "AppAvatar";

// Colori: pochi e con contrasto pieno, si guarda da lontano e spesso di sbieco.
#define COLORE_FONDO         lv_color_hex(0x101418)
#define COLORE_PALCO         lv_color_hex(0x1b2129)
#define COLORE_ACCENTO       lv_color_hex(0x2ec4b6)
#define COLORE_TESTO         lv_color_hex(0xf0f8f7)
#define COLORE_TESTO_TENUE   lv_color_hex(0x93a1a8)
#define COLORE_ALLARME       lv_color_hex(0xe05c4b)

#define BORDO                12
// Il tasto si preme col pollice, spesso senza guardare: grande.
#define PTT_ALTEZZA          120

// Lo schermo e' verticale (480x800): il palco sta sopra, i comandi sotto, invece
// di affiancarli come si farebbe su un display orizzontale.
#define PALCO_ALTEZZA_PCT    46

AppAvatar::AppAvatar():
    ESP_Brookesia_PhoneApp("Avatar", &img_app_avatar, true)
{
}

AppAvatar::~AppAvatar()
{
}

bool AppAvatar::run(void)
{
    lv_area_t area = getVisualArea();
    _width = area.x2 - area.x1;
    _height = area.y2 - area.y1;

    costruisciInterfaccia();

    // Il client resta in piedi da solo e riconnette: si avvia qui e la UI segue
    // gli eventi. Se la rete non c'e' ancora, riprovera' da se'.
    const esp_err_t err = gateway_client_start(CONFIG_AVATAR_GATEWAY_URI,
                                               CONFIG_AVATAR_GATEWAY_TOKEN,
                                               (gateway_event_cb_t)gateway_cb, this);
    if (err == ESP_OK || err == ESP_ERR_INVALID_STATE) {
        mostraStato(gateway_client_is_ready() ? Stato::PRONTO : Stato::COLLEGAMENTO,
                    CONFIG_AVATAR_GATEWAY_URI);
    } else {
        ESP_LOGE(TAG, "avvio del client gateway fallito (%d)", err);
        mostraStato(Stato::ERRORE, "client non avviato");
    }

    return true;
}

void AppAvatar::costruisciInterfaccia(void)
{
    lv_obj_t *radice = lv_scr_act();
    lv_obj_set_style_bg_color(radice, COLORE_FONDO, 0);
    lv_obj_set_style_bg_opa(radice, LV_OPA_COVER, 0);

    const int utile_w = _width - BORDO * 2;
    const int palco_h = _height * PALCO_ALTEZZA_PCT / 100;

    // --- il palco: qui andra' il video della persona, per ora il poster ---
    _palco = lv_obj_create(radice);
    lv_obj_set_size(_palco, utile_w, palco_h);
    lv_obj_align(_palco, LV_ALIGN_TOP_MID, 0, BORDO);
    lv_obj_set_style_bg_color(_palco, COLORE_PALCO, 0);
    lv_obj_set_style_border_width(_palco, 0, 0);
    lv_obj_set_style_radius(_palco, 12, 0);
    lv_obj_clear_flag(_palco, LV_OBJ_FLAG_SCROLLABLE);

    _palco_testo = lv_label_create(_palco);
    lv_label_set_text(_palco_testo, "La persona compare qui");
    lv_obj_set_style_text_color(_palco_testo, COLORE_TESTO_TENUE, 0);
    lv_obj_set_style_text_font(_palco_testo, &lv_font_montserrat_20, 0);
    lv_obj_center(_palco_testo);

    // --- riga di stato: pallino + testo, cosi' non dipende dal solo colore ---
    _spia = lv_obj_create(radice);
    lv_obj_set_size(_spia, 16, 16);
    lv_obj_align(_spia, LV_ALIGN_TOP_LEFT, BORDO, BORDO + palco_h + BORDO + 4);
    lv_obj_set_style_radius(_spia, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(_spia, 0, 0);
    lv_obj_clear_flag(_spia, LV_OBJ_FLAG_SCROLLABLE);

    _stato_label = lv_label_create(radice);
    lv_obj_align(_stato_label, LV_ALIGN_TOP_LEFT, BORDO + 26,
                 BORDO + palco_h + BORDO);
    lv_obj_set_width(_stato_label, utile_w - 26);
    lv_label_set_long_mode(_stato_label, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(_stato_label, COLORE_TESTO, 0);
    lv_obj_set_style_text_font(_stato_label, &lv_font_montserrat_18, 0);

    // --- la conversazione: cosa ha capito e cosa risponde ---
    _trascrizione = lv_label_create(radice);
    lv_obj_set_width(_trascrizione, utile_w);
    lv_obj_align(_trascrizione, LV_ALIGN_TOP_LEFT, BORDO,
                 BORDO + palco_h + BORDO + 56);
    lv_label_set_long_mode(_trascrizione, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(_trascrizione, COLORE_TESTO_TENUE, 0);
    lv_obj_set_style_text_font(_trascrizione, &lv_font_montserrat_16, 0);
    lv_label_set_text(_trascrizione, "");

    // --- il tasto, in fondo dove arriva il pollice ---
    _ptt = lv_btn_create(radice);
    lv_obj_set_size(_ptt, utile_w, PTT_ALTEZZA);
    lv_obj_align(_ptt, LV_ALIGN_BOTTOM_MID, 0, -BORDO);
    lv_obj_set_style_bg_color(_ptt, COLORE_ACCENTO, 0);
    lv_obj_set_style_radius(_ptt, 12, 0);
    lv_obj_add_event_cb(_ptt, ptt_event_cb, LV_EVENT_CLICKED, this);

    lv_obj_t *ptt_testo = lv_label_create(_ptt);
    // Senza microfono il tasto manda un turno scritto: il testo lo dice, invece
    // di promettere un "tieni premuto e parla" che ancora non funziona.
    lv_label_set_text(ptt_testo, "Prova a parlare\ncon l'agente");
    lv_obj_set_style_text_align(ptt_testo, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(ptt_testo, lv_color_hex(0x08211f), 0);
    lv_obj_set_style_text_font(ptt_testo, &lv_font_montserrat_22, 0);
    lv_obj_center(ptt_testo);
}

void AppAvatar::mostraStato(Stato stato, const char *dettaglio)
{
    _stato = stato;

    const char *testo = "";
    lv_color_t colore = COLORE_TESTO_TENUE;

    switch (stato) {
    case Stato::SCOLLEGATO:
        testo = "Non collegato";
        break;
    case Stato::COLLEGAMENTO:
        testo = "Mi collego...";
        colore = COLORE_ACCENTO;
        break;
    case Stato::PRONTO:
        testo = "Pronto";
        colore = COLORE_ACCENTO;
        break;
    case Stato::ASCOLTO:
        testo = "Ti ascolto";
        colore = COLORE_ACCENTO;
        break;
    case Stato::PENSA:
        testo = "Sto pensando";
        colore = COLORE_ACCENTO;
        break;
    case Stato::PARLA:
        testo = "Sta parlando";
        colore = COLORE_ACCENTO;
        break;
    case Stato::ERRORE:
        testo = "Errore";
        colore = COLORE_ALLARME;
        break;
    }

    if (_spia) {
        lv_obj_set_style_bg_color(_spia, colore, 0);
    }
    if (_stato_label) {
        if (dettaglio && dettaglio[0]) {
            lv_label_set_text_fmt(_stato_label, "%s — %s", testo, dettaglio);
        } else {
            lv_label_set_text(_stato_label, testo);
        }
    }
}

void AppAvatar::mostraTesto(const char *testo)
{
    if (_trascrizione && testo) {
        lv_label_set_text(_trascrizione, testo);
    }
}

void AppAvatar::avviaTurno(void)
{
    if (!gateway_client_is_ready()) {
        mostraStato(Stato::ERRORE, "gateway non raggiungibile");
        return;
    }

    _risposta.clear();
    mostraStato(Stato::PENSA);
    mostraTesto("");

    // Prima si apre il turno, poi si manda il contenuto: i messaggi fuori turno
    // il gateway li scarta in silenzio.
    gateway_turn_start("hold", CONFIG_AVATAR_LANG, nullptr, 16000);
    // Al posto dell'audio del microfono, che ancora non c'e'.
    gateway_send_text(CONFIG_AVATAR_TEST_PHRASE);
}

void AppAvatar::ptt_event_cb(lv_event_t *e)
{
    AppAvatar *app = (AppAvatar *)lv_event_get_user_data(e);
    if (app != nullptr) {
        app->avviaTurno();
    }
}

void AppAvatar::gateway_cb(const void *event, void *user_ctx)
{
    const gateway_event_t *ev = (const gateway_event_t *)event;
    AppAvatar *app = (AppAvatar *)user_ctx;
    if (ev == nullptr || app == nullptr) {
        return;
    }

    // Questa callback gira nel task del WebSocket, non in quello di LVGL: ogni
    // tocco alla UI va fatto col lock, altrimenti si corrompe il rendering.
    if (esp_lv_adapter_lock(200) != ESP_OK) {
        ESP_LOGW(TAG, "LVGL occupato: evento %d non mostrato", (int)ev->type);
        return;
    }

    switch (ev->type) {
    case GATEWAY_EV_CONNECTED:
        app->mostraStato(Stato::COLLEGAMENTO, "autenticazione");
        break;
    case GATEWAY_EV_READY:
        app->mostraStato(Stato::PRONTO);
        break;
    case GATEWAY_EV_TRANSCRIPT:
        app->mostraStato(Stato::PENSA);
        if (ev->text && ev->text[0]) {
            lv_label_set_text_fmt(app->_trascrizione, "Tu: %s", ev->text);
        }
        break;
    case GATEWAY_EV_REPLY:
        // La risposta arriva a pezzi: si accumula e si riscrive l'etichetta.
        if (ev->text) {
            app->_risposta += ev->text;
            lv_label_set_text_fmt(app->_trascrizione, "%s", app->_risposta.c_str());
        }
        break;
    case GATEWAY_EV_AUDIO_START:
        app->mostraStato(Stato::PARLA, "audio in arrivo");
        break;
    case GATEWAY_EV_AUDIO_DATA:
        // Senza altoparlante non c'e' dove riprodurlo: per ora si conta, cosi'
        // si vede che il percorso audio funziona davvero.
        break;
    case GATEWAY_EV_AUDIO_END:
        break;
    case GATEWAY_EV_DONE:
        app->mostraStato(Stato::PRONTO);
        break;
    case GATEWAY_EV_ERROR:
        app->mostraStato(Stato::ERRORE, ev->text);
        break;
    case GATEWAY_EV_DISCONNECTED:
        app->mostraStato(Stato::SCOLLEGATO, "riprovo");
        break;
    }

    esp_lv_adapter_unlock();
}

bool AppAvatar::back(void)
{
    // Il gesto "indietro" chiude l'app: il sistema si aspetta questa chiamata.
    return notifyCoreClosed();
}

bool AppAvatar::close(void)
{
    // La connessione si lascia aperta: l'app si chiude e si riapre spesso, e
    // rifare auth ogni volta non serve a niente.
    _palco = nullptr;
    _palco_testo = nullptr;
    _spia = nullptr;
    _stato_label = nullptr;
    _ptt = nullptr;
    _trascrizione = nullptr;
    return true;
}
