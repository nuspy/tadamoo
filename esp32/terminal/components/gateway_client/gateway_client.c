#include "gateway_client.h"

#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "gateway";

// Il gateway pretende l'auth entro 10 s dall'apertura: si manda subito, dentro
// l'evento di connessione, senza passare da altri task.
#define AUTH_TIMEOUT_MS   10000
#define SEND_TIMEOUT_MS   2000

static struct {
    esp_websocket_client_handle_t ws;
    gateway_event_cb_t cb;
    void *user_ctx;
    char token[128];
    volatile bool ready;
    volatile bool receiving_audio;
    SemaphoreHandle_t lock;
} s_gw;

static void notify(gateway_event_type_t type, const char *text, int sample_rate,
                   const void *audio, size_t audio_len)
{
    if (s_gw.cb == NULL) {
        return;
    }
    const gateway_event_t ev = {
        .type = type,
        .text = text,
        .sample_rate = sample_rate,
        .audio = audio,
        .audio_len = audio_len,
    };
    s_gw.cb(&ev, s_gw.user_ctx);
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

static void handle_text_message(const char *data, int len)
{
    cJSON *root = cJSON_ParseWithLength(data, (size_t)len);
    if (root == NULL) {
        ESP_LOGW(TAG, "messaggio non JSON, ignorato");
        return;
    }
    const cJSON *tipo = cJSON_GetObjectItemCaseSensitive(root, "type");
    if (!cJSON_IsString(tipo)) {
        cJSON_Delete(root);
        return;
    }
    const char *t = tipo->valuestring;

    if (strcmp(t, "ready") == 0) {
        s_gw.ready = true;
        ESP_LOGI(TAG, "gateway pronto");
        notify(GATEWAY_EV_READY, NULL, 0, NULL, 0);
    } else if (strcmp(t, "transcript") == 0) {
        const cJSON *txt = cJSON_GetObjectItemCaseSensitive(root, "text");
        notify(GATEWAY_EV_TRANSCRIPT, cJSON_IsString(txt) ? txt->valuestring : "", 0, NULL, 0);
    } else if (strcmp(t, "reply") == 0) {
        const cJSON *chunk = cJSON_GetObjectItemCaseSensitive(root, "chunk");
        notify(GATEWAY_EV_REPLY, cJSON_IsString(chunk) ? chunk->valuestring : "", 0, NULL, 0);
    } else if (strcmp(t, "audio_start") == 0) {
        const cJSON *sr = cJSON_GetObjectItemCaseSensitive(root, "sample_rate");
        // Il sample rate NON è costante: 24000 di solito, ma con alcune voci
        // (XTTS) arriva 22050. Va letto ogni volta, non dato per scontato.
        const int rate = cJSON_IsNumber(sr) ? sr->valueint : 24000;
        s_gw.receiving_audio = true;
        notify(GATEWAY_EV_AUDIO_START, NULL, rate, NULL, 0);
    } else if (strcmp(t, "audio_end") == 0) {
        s_gw.receiving_audio = false;
        notify(GATEWAY_EV_AUDIO_END, NULL, 0, NULL, 0);
    } else if (strcmp(t, "done") == 0) {
        notify(GATEWAY_EV_DONE, NULL, 0, NULL, 0);
    } else if (strcmp(t, "error") == 0) {
        const cJSON *msg = cJSON_GetObjectItemCaseSensitive(root, "message");
        const char *m = cJSON_IsString(msg) ? msg->valuestring : "errore";
        ESP_LOGW(TAG, "errore dal gateway: %s", m);
        notify(GATEWAY_EV_ERROR, m, 0, NULL, 0);
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
        cJSON *auth = cJSON_CreateObject();
        if (auth) {
            cJSON_AddStringToObject(auth, "type", "auth");
            cJSON_AddStringToObject(auth, "token", s_gw.token);
            send_json(auth);
        }
        notify(GATEWAY_EV_CONNECTED, NULL, 0, NULL, 0);
        break;
    }
    case WEBSOCKET_EVENT_DISCONNECTED:
        s_gw.ready = false;
        s_gw.receiving_audio = false;
        ESP_LOGW(TAG, "disconnesso");
        notify(GATEWAY_EV_DISCONNECTED, NULL, 0, NULL, 0);
        break;

    case WEBSOCKET_EVENT_DATA:
        if (data->op_code == 0x2) {                 /* frame binario: audio */
            notify(GATEWAY_EV_AUDIO_DATA, NULL, 0, data->data_ptr, (size_t)data->data_len);
        } else if (data->op_code == 0x1) {          /* frame di testo: JSON */
            handle_text_message(data->data_ptr, data->data_len);
        }
        /* op_code 0x8/0x9/0xA (close/ping/pong) li gestisce il client. */
        break;

    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGW(TAG, "errore di trasporto");
        break;

    default:
        break;
    }
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

    s_gw.cb = cb;
    s_gw.user_ctx = user_ctx;
    s_gw.ready = false;
    snprintf(s_gw.token, sizeof(s_gw.token), "%s", token ? token : "");

    const esp_websocket_client_config_t cfg = {
        .uri = uri,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms = AUTH_TIMEOUT_MS,
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
