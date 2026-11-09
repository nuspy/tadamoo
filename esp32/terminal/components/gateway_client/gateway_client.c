#include "gateway_client.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "viseme_timeline.h"
#include "ws_reasm.h"

static const char *TAG = "gateway";

// Il gateway pretende l'auth entro 10 s dall'apertura: si manda subito, dentro
// l'evento di connessione, senza passare da altri task.
#define AUTH_TIMEOUT_MS   10000
#define SEND_TIMEOUT_MS   2000

#define MAX_RENDER_MODES  4
#define MODE_NAME_MAX     16
// A message larger than this is dropped whole (a timeline of VT_MAX_ITEMS items
// is about 20 KB). The buffer is allocated once, in PSRAM when there is one.
#define MSG_BUF_CAP       (32 * 1024)
#define WS_BUFFER_SIZE    4096

static struct {
    esp_websocket_client_handle_t ws;
    gateway_event_cb_t cb;
    void *user_ctx;
    char token[128];
    volatile bool ready;
    volatile bool receiving_audio;
    SemaphoreHandle_t lock;
    char modes[MAX_RENDER_MODES][MODE_NAME_MAX];
    size_t n_modes;
    ws_reasm_t reasm;
    char *msg_buf;
    vt_segment_t *seg_buf;
} s_gw;

static void emit(const gateway_event_t *ev)
{
    if (s_gw.cb != NULL) {
        s_gw.cb(ev, s_gw.user_ctx);
    }
}

static void notify(gateway_event_type_t type, const char *text, int sample_rate,
                   const void *audio, size_t audio_len)
{
    const gateway_event_t ev = {
        .type = type,
        .text = text,
        .sample_rate = sample_rate,
        .audio = audio,
        .audio_len = audio_len,
    };
    emit(&ev);
}

static esp_err_t send_json_raw(const char *json)
{
    if (s_gw.ws == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    const int len = (int)strlen(json);
    const int sent = esp_websocket_client_send_text(s_gw.ws, json, len,
                                                   pdMS_TO_TICKS(SEND_TIMEOUT_MS));
    return (sent == len) ? ESP_OK : ESP_FAIL;
}

/** Costruisce e invia un oggetto JSON, liberandolo sempre. */
static esp_err_t send_json(cJSON *obj)
{
    if (obj == NULL) {
        return ESP_ERR_NO_MEM;
    }
    char *testo = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    if (testo == NULL) {
        return ESP_ERR_NO_MEM;
    }
    const esp_err_t err = send_json_raw(testo);
    cJSON_free(testo);
    return err;
}

static void handle_visemes(const char *data, size_t len)
{
    if (s_gw.seg_buf == NULL) {
        return;
    }
    const int rc = vt_parse_message(data, len, s_gw.seg_buf);
    if (rc != VT_OK) {
        ESP_LOGW(TAG, "timeline dei visemi non valida (%d), ignorata", rc);
        return;
    }
    if (s_gw.seg_buf->truncated) {
        ESP_LOGW(TAG, "timeline dei visemi oltre %d voci, tagliata", VT_MAX_ITEMS);
    }
    const gateway_event_t ev = {
        .type = GATEWAY_EV_VISEMES,
        .segment = s_gw.seg_buf->segment,
        .visemes = s_gw.seg_buf,
    };
    emit(&ev);
}

static const char *mode_of(const cJSON *root)
{
    const cJSON *m = cJSON_GetObjectItemCaseSensitive(root, "render_mode");
    return (cJSON_IsString(m) && m->valuestring != NULL && m->valuestring[0] != '\0') ? m->valuestring : "none";
}

static void handle_text_message(const char *data, size_t len)
{
    // The timeline is the one large message: it is read without building a
    // JSON tree, so its size does not cost heap.
    char kind[16];
    if (vt_peek_type(data, len, kind, sizeof(kind)) && strcmp(kind, "visemes") == 0) {
        handle_visemes(data, len);
        return;
    }

    cJSON *root = cJSON_ParseWithLength(data, len);
    if (root == NULL) {
        ESP_LOGW(TAG, "messaggio non JSON, ignorato");
        return;
    }
    const cJSON *tipo = cJSON_GetObjectItemCaseSensitive(root, "type");
    if (!cJSON_IsString(tipo) || tipo->valuestring == NULL) {
        cJSON_Delete(root);
        return;
    }
    const char *t = tipo->valuestring;
    gateway_event_t ev = {0};

    if (strcmp(t, "ready") == 0) {
        s_gw.ready = true;
        ESP_LOGI(TAG, "gateway pronto");
        ev.type = GATEWAY_EV_READY;
        ev.mode = mode_of(root);
        emit(&ev);
    } else if (strcmp(t, "render_mode") == 0) {
        ev.type = GATEWAY_EV_RENDER_MODE;
        ev.mode = mode_of(root);
        emit(&ev);
    } else if (strcmp(t, "interrupt") == 0) {
        ev.type = GATEWAY_EV_INTERRUPT;
        emit(&ev);
    } else if (strcmp(t, "transcript") == 0) {
        const cJSON *txt = cJSON_GetObjectItemCaseSensitive(root, "text");
        ev.type = GATEWAY_EV_TRANSCRIPT;
        ev.text = cJSON_IsString(txt) ? txt->valuestring : "";
        emit(&ev);
    } else if (strcmp(t, "reply") == 0) {
        const cJSON *chunk = cJSON_GetObjectItemCaseSensitive(root, "chunk");
        ev.type = GATEWAY_EV_REPLY;
        ev.text = cJSON_IsString(chunk) ? chunk->valuestring : "";
        emit(&ev);
    } else if (strcmp(t, "audio_start") == 0) {
        const cJSON *sr = cJSON_GetObjectItemCaseSensitive(root, "sample_rate");
        const cJSON *seg = cJSON_GetObjectItemCaseSensitive(root, "segment");
        // Il sample rate NON è costante: 24000 di solito, ma con alcune voci
        // (XTTS) arriva 22050. Va letto ogni volta, non dato per scontato.
        ev.type = GATEWAY_EV_AUDIO_START;
        ev.sample_rate = cJSON_IsNumber(sr) ? sr->valueint : 24000;
        ev.segment = cJSON_IsNumber(seg) ? seg->valueint : 0;
        s_gw.receiving_audio = true;
        emit(&ev);
    } else if (strcmp(t, "audio_end") == 0) {
        s_gw.receiving_audio = false;
        ev.type = GATEWAY_EV_AUDIO_END;
        emit(&ev);
    } else if (strcmp(t, "done") == 0) {
        ev.type = GATEWAY_EV_DONE;
        emit(&ev);
    } else if (strcmp(t, "error") == 0) {
        const cJSON *msg = cJSON_GetObjectItemCaseSensitive(root, "message");
        const char *m = cJSON_IsString(msg) ? msg->valuestring : "errore";
        ESP_LOGW(TAG, "errore dal gateway: %s", m);
        ev.type = GATEWAY_EV_ERROR;
        ev.text = m;
        emit(&ev);
    } else {
        ESP_LOGD(TAG, "messaggio '%s' non gestito", t);
    }
    cJSON_Delete(root);
}

static void ws_event_handler(void *arg, esp_event_base_t base, int32_t event_id, void *event_data)
{
    (void)arg;
    (void)base;
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;

    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED: {
        ESP_LOGI(TAG, "connesso, mando auth");
        ws_reasm_reset(&s_gw.reasm);
        cJSON *auth = cJSON_CreateObject();
        if (auth) {
            cJSON_AddStringToObject(auth, "type", "auth");
            cJSON_AddStringToObject(auth, "token", s_gw.token);
            cJSON *modes = cJSON_AddArrayToObject(auth, "render_modes");
            for (size_t i = 0; modes != NULL && i < s_gw.n_modes; i++) {
                cJSON_AddItemToArray(modes, cJSON_CreateString(s_gw.modes[i]));
            }
            cJSON_AddStringToObject(auth, "device", "esp32");
            send_json(auth);
        }
        notify(GATEWAY_EV_CONNECTED, NULL, 0, NULL, 0);
        break;
    }
    case WEBSOCKET_EVENT_DISCONNECTED:
        s_gw.ready = false;
        s_gw.receiving_audio = false;
        ws_reasm_reset(&s_gw.reasm);
        ESP_LOGW(TAG, "disconnesso");
        notify(GATEWAY_EV_DISCONNECTED, NULL, 0, NULL, 0);
        break;

    case WEBSOCKET_EVENT_DATA: {
        // A frame arrives in pieces of at most WS_BUFFER_SIZE: the text ones
        // are put together before they are read. Close, ping and pong
        // (op_code 0x8, 0x9, 0xA) are handled by the client.
        if (data->data_len < 0 || data->payload_len < 0 || data->payload_offset < 0) {
            break;
        }
        const char *out = NULL;
        size_t out_len = 0;
        switch (ws_reasm_feed(&s_gw.reasm, data->op_code, data->data_ptr, (size_t)data->data_len,
                              (size_t)data->payload_len, (size_t)data->payload_offset, data->fin,
                              &out, &out_len)) {
        case WSR_BINARY:                            /* audio PCM16 */
            notify(GATEWAY_EV_AUDIO_DATA, NULL, 0, out, out_len);
            break;
        case WSR_TEXT:                              /* JSON */
            handle_text_message(out, out_len);
            break;
        case WSR_DROPPED:
            ESP_LOGW(TAG, "messaggio oltre %d byte scartato", MSG_BUF_CAP);
            break;
        case WSR_NONE:
        default:
            break;
        }
        break;
    }

    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGW(TAG, "errore di trasporto");
        break;

    default:
        break;
    }
}

esp_err_t gateway_client_set_render_modes(const char *const *modes, size_t count)
{
    if (s_gw.ws != NULL || count > MAX_RENDER_MODES || (count > 0 && modes == NULL)) {
        return ESP_ERR_INVALID_STATE;
    }
    char tmp[MAX_RENDER_MODES][MODE_NAME_MAX];
    for (size_t i = 0; i < count; i++) {
        const char *m = modes[i];
        const size_t n = (m != NULL) ? strlen(m) : 0;
        if (n == 0 || n >= MODE_NAME_MAX) {
            return ESP_ERR_INVALID_ARG;
        }
        for (size_t k = 0; k < n; k++) {
            const char c = m[k];
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-')) {
                return ESP_ERR_INVALID_ARG;
            }
        }
        memcpy(tmp[i], m, n + 1);
    }
    memcpy(s_gw.modes, tmp, sizeof(tmp));
    s_gw.n_modes = count;
    return ESP_OK;
}

esp_err_t gateway_client_start(const char *uri, const char *token,
                               gateway_event_cb_t cb, void *user_ctx)
{
    if (uri == NULL || uri[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_gw.ws != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_gw.lock == NULL) {
        s_gw.lock = xSemaphoreCreateMutex();
        if (s_gw.lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }

    if (s_gw.msg_buf == NULL) {
        s_gw.msg_buf = heap_caps_malloc(MSG_BUF_CAP, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_gw.msg_buf == NULL) {
            s_gw.msg_buf = heap_caps_malloc(MSG_BUF_CAP, MALLOC_CAP_8BIT);
        }
    }
    if (s_gw.seg_buf == NULL) {
        s_gw.seg_buf = heap_caps_malloc(sizeof(vt_segment_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_gw.seg_buf == NULL) {
            s_gw.seg_buf = heap_caps_malloc(sizeof(vt_segment_t), MALLOC_CAP_8BIT);
        }
    }
    if (s_gw.msg_buf == NULL || s_gw.seg_buf == NULL) {
        // Without the buffers a long message cannot be read: the client still
        // works for the short ones, and a timeline is ignored (voice only).
        ESP_LOGW(TAG, "buffer dei messaggi non allocati");
    }
    ws_reasm_init(&s_gw.reasm, s_gw.msg_buf, s_gw.msg_buf ? MSG_BUF_CAP : 0);

    s_gw.cb = cb;
    s_gw.user_ctx = user_ctx;
    s_gw.ready = false;
    snprintf(s_gw.token, sizeof(s_gw.token), "%s", token ? token : "");

    const esp_websocket_client_config_t cfg = {
        .uri = uri,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms = AUTH_TIMEOUT_MS,
        .buffer_size = WS_BUFFER_SIZE,
        // Il telefono e il PC possono sparire e tornare: la riconnessione
        // automatica evita di dover gestire a mano ogni caduta.
        .disable_auto_reconnect = false,
        .task_stack = 6144,
    };
    s_gw.ws = esp_websocket_client_init(&cfg);
    if (s_gw.ws == NULL) {
        return ESP_FAIL;
    }
    ESP_ERROR_CHECK(esp_websocket_register_events(s_gw.ws, WEBSOCKET_EVENT_ANY,
                                                 ws_event_handler, NULL));
    ESP_LOGI(TAG, "mi collego a %s", uri);
    return esp_websocket_client_start(s_gw.ws);
}

esp_err_t gateway_client_stop(void)
{
    if (s_gw.ws == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    s_gw.ready = false;
    esp_websocket_client_close(s_gw.ws, pdMS_TO_TICKS(SEND_TIMEOUT_MS));
    esp_websocket_client_destroy(s_gw.ws);
    s_gw.ws = NULL;
    s_gw.cb = NULL;
    return ESP_OK;
}

bool gateway_client_is_ready(void)
{
    return s_gw.ready && s_gw.ws != NULL && esp_websocket_client_is_connected(s_gw.ws);
}

esp_err_t gateway_turn_start(const char *mode, const char *lang,
                             const char *voice, int sample_rate)
{
    if (!gateway_client_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    cJSON *msg = cJSON_CreateObject();
    if (msg == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(msg, "type", "start");
    cJSON_AddStringToObject(msg, "mode", mode ? mode : "hold");
    if (lang && lang[0]) {
        cJSON_AddStringToObject(msg, "lang", lang);
    }
    // Nota: il gateway ignora `voice` quando c'è una persona attiva.
    if (voice && voice[0]) {
        cJSON_AddStringToObject(msg, "voice", voice);
    }
    cJSON_AddNumberToObject(msg, "sample_rate", sample_rate > 0 ? sample_rate : 16000);
    return send_json(msg);
}

esp_err_t gateway_send_audio(const void *pcm, size_t len)
{
    if (!gateway_client_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (pcm == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len % 2 != 0) {
        // Sono campioni int16: con un frame dispari il gateway solleva e la
        // connessione cade. Meglio rifiutarlo qui.
        ESP_LOGE(TAG, "frame audio di lunghezza dispari (%u): scartato", (unsigned)len);
        return ESP_ERR_INVALID_SIZE;
    }
    const int sent = esp_websocket_client_send_bin(s_gw.ws, (const char *)pcm, (int)len,
                                                  pdMS_TO_TICKS(SEND_TIMEOUT_MS));
    return (sent == (int)len) ? ESP_OK : ESP_FAIL;
}

esp_err_t gateway_turn_end(void)
{
    if (!gateway_client_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    cJSON *msg = cJSON_CreateObject();
    if (msg == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(msg, "type", "end");
    return send_json(msg);
}

esp_err_t gateway_send_text(const char *text)
{
    if (!gateway_client_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (text == NULL || text[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    cJSON *msg = cJSON_CreateObject();
    if (msg == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(msg, "type", "text");
    cJSON_AddStringToObject(msg, "text", text);
    ESP_LOGI(TAG, "turno scritto: \"%s\"", text);
    return send_json(msg);
}

esp_err_t gateway_send_interrupt(void)
{
    if (!gateway_client_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    cJSON *msg = cJSON_CreateObject();
    if (msg == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(msg, "type", "interrupt");
    return send_json(msg);
}

esp_err_t gateway_send_render_failed(const char *mode, const char *why)
{
    if (!gateway_client_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (mode == NULL || mode[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    cJSON *msg = cJSON_CreateObject();
    if (msg == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(msg, "type", "render_failed");
    cJSON_AddStringToObject(msg, "mode", mode);
    cJSON_AddStringToObject(msg, "why", (why != NULL && why[0] != '\0') ? why : "failed");
    ESP_LOGW(TAG, "modo '%s' non disegnabile: %s", mode, (why != NULL) ? why : "failed");
    return send_json(msg);
}
