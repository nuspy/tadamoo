#include "AvatarFace.hpp"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "AvatarFace";

#define COLORE_PELLE      lv_color_hex(0xe8c39e)
#define COLORE_OCCHI      lv_color_hex(0x1b2129)
#define COLORE_BOCCA      lv_color_hex(0x5a1f2b)

// 25 updates a second is far finer than the 60 ms cross-fade needs.
#define TICK_MS           40
#define MARGINE           12
// A closed mouth is still a visible line.
#define BOCCA_H_MIN       6

static lv_obj_t *makeShape(lv_obj_t *parent, lv_color_t color)
{
    lv_obj_t *obj = lv_obj_create(parent);
    if (obj == nullptr) {
        return nullptr;
    }
    lv_obj_clear_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(obj, color, 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(obj, 0, 0);
    lv_obj_set_style_pad_all(obj, 0, 0);
    lv_obj_set_style_shadow_width(obj, 0, 0);
    lv_obj_set_style_radius(obj, LV_RADIUS_CIRCLE, 0);
    return obj;
}

AvatarFace::~AvatarFace()
{
    end();
    if (_player != nullptr) {
        heap_caps_free(_player);
        _player = nullptr;
    }
    if (_mutex != nullptr) {
        vSemaphoreDelete(_mutex);
        _mutex = nullptr;
    }
}

bool AvatarFace::begin(lv_obj_t *stage, int width, int height)
{
    end();
    if (stage == nullptr || width < 64 || height < 64) {
        ESP_LOGE(TAG, "palco non valido (%dx%d)", width, height);
        return false;
    }
    if (_mutex == nullptr) {
        _mutex = xSemaphoreCreateMutex();
    }
    if (_player == nullptr) {
        _player = (vt_player_t *)heap_caps_calloc(1, sizeof(vt_player_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (_player == nullptr) {
            _player = (vt_player_t *)heap_caps_calloc(1, sizeof(vt_player_t), MALLOC_CAP_8BIT);
        }
    }
    if (_mutex == nullptr || _player == nullptr) {
        ESP_LOGE(TAG, "memoria insufficiente per il clock dei visemi");
        return false;
    }

    // The code that decides what is drawn is checked before it is relied on.
    const int check = vt_selftest(_player);
    if (check != 0) {
        ESP_LOGE(TAG, "controllo dei visemi fallito (%d)", check);
        return false;
    }

    const int d = ((width < height) ? width : height) - 2 * MARGINE;
    _head = makeShape(stage, COLORE_PELLE);
    if (_head == nullptr) {
        return false;
    }
    lv_obj_set_size(_head, d, d);
    lv_obj_align(_head, LV_ALIGN_CENTER, 0, 0);

    const int eye = d / 10 > 8 ? d / 10 : 8;
    const int eye_dx = d / 5;
    const int eye_dy = -d * 14 / 100;
    for (int side = -1; side <= 1; side += 2) {
        lv_obj_t *e = makeShape(_head, COLORE_OCCHI);
        if (e == nullptr) {
            lv_obj_del(_head);
            _head = nullptr;
            return false;
        }
        lv_obj_set_size(e, eye, eye);
        lv_obj_align(e, LV_ALIGN_CENTER, side * eye_dx, eye_dy);
    }

    _mouth_w0 = d * 38 / 100;
    _mouth_dy = d * 22 / 100;
    _mouth = makeShape(_head, COLORE_BOCCA);
    if (_mouth == nullptr) {
        lv_obj_del(_head);
        _head = nullptr;
        return false;
    }
    _last_w = _last_h = -1;
    apply(vt_mouth_of(VT_SIL));

    _timer = lv_timer_create(tickCb, TICK_MS, this);
    if (_timer == nullptr) {
        lv_obj_del(_head);
        _head = nullptr;
        _mouth = nullptr;
        return false;
    }

    lv_obj_add_flag(_head, LV_OBJ_FLAG_HIDDEN);
    stop();
    _ready = true;
    return true;
}

void AvatarFace::end()
{
    _ready = false;
    _active = false;
    if (_timer != nullptr) {
        lv_timer_del(_timer);
        _timer = nullptr;
    }
    _head = nullptr;
    _mouth = nullptr;
    stop();
}

void AvatarFace::setActive(bool active)
{
    if (!_ready) {
        return;
    }
    if (!active) {
        _active = false;
        stop();
    }
    if (_head != nullptr) {
        if (active) {
            lv_obj_clear_flag(_head, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(_head, LV_OBJ_FLAG_HIDDEN);
        }
    }
    if (active) {
        _active = true;
    }
}

void AvatarFace::replyStart(int sample_rate)
{
    if (!_active || _player == nullptr) {
        return;
    }
    xSemaphoreTake(_mutex, portMAX_DELAY);
    vt_player_begin_reply(_player, sample_rate > 0 ? (uint32_t)sample_rate : 0);
    xSemaphoreGive(_mutex);
}

void AvatarFace::pcm(size_t bytes)
{
    if (!_active || _player == nullptr) {
        return;
    }
    xSemaphoreTake(_mutex, portMAX_DELAY);
    vt_player_on_pcm(_player, bytes, (uint64_t)esp_timer_get_time());
    xSemaphoreGive(_mutex);
}

void AvatarFace::timeline(const vt_segment_t *segment)
{
    if (!_active || _player == nullptr || segment == nullptr) {
        return;
    }
    xSemaphoreTake(_mutex, portMAX_DELAY);
    if (!vt_player_add_segment(_player, segment)) {
        ESP_LOGW(TAG, "timeline %d scartata (%u scartate, %u rifiutate)", (int)segment->segment,
                 (unsigned)_player->dropped, (unsigned)_player->refused);
    }
    xSemaphoreGive(_mutex);
}

void AvatarFace::replyEnd()
{
    if (!_active || _player == nullptr) {
        return;
    }
    xSemaphoreTake(_mutex, portMAX_DELAY);
    vt_player_end_reply(_player);
    xSemaphoreGive(_mutex);
}

void AvatarFace::stop()
{
    if (_player == nullptr || _mutex == nullptr) {
        return;
    }
    xSemaphoreTake(_mutex, portMAX_DELAY);
    vt_player_reset(_player);
    xSemaphoreGive(_mutex);
}

void AvatarFace::played(uint64_t played_us)
{
    if (!_active || _player == nullptr) {
        return;
    }
    xSemaphoreTake(_mutex, portMAX_DELAY);
    vt_player_set_played_us(_player, played_us);
    xSemaphoreGive(_mutex);
}

void AvatarFace::tickCb(lv_timer_t *timer)
{
    AvatarFace *face = (AvatarFace *)timer->user_data;
    if (face != nullptr) {
        face->tick();
    }
}

void AvatarFace::tick()
{
    if (!_ready || _mouth == nullptr) {
        return;
    }
    vt_mouth_t mouth = vt_mouth_of(VT_SIL);
    if (_active && _player != nullptr) {
        xSemaphoreTake(_mutex, portMAX_DELAY);
        mouth = vt_player_sample(_player, (uint64_t)esp_timer_get_time(), nullptr);
        xSemaphoreGive(_mutex);
    }
    apply(mouth);
}

void AvatarFace::apply(vt_mouth_t mouth)
{
    if (_mouth == nullptr) {
        return;
    }
    const int w = _mouth_w0 * mouth.wide_pm / 1000;
    int h = _mouth_w0 * mouth.open_pm / 1000;
    if (h < BOCCA_H_MIN) {
        h = BOCCA_H_MIN;
    }
    if (w == _last_w && h == _last_h) {
        return;
    }
    _last_w = w;
    _last_h = h;
    lv_obj_set_size(_mouth, w, h);
    lv_obj_align(_mouth, LV_ALIGN_CENTER, 0, _mouth_dy);
}
