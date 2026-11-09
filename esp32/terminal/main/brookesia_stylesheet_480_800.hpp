/*
 * Stylesheet ESP-Brookesia per il pannello 480x800 verticale della Guition
 * JC4880P443C_I_W.
 *
 * esp-brookesia 0.4.1 porta stylesheet per 320x240, 320x480, 480x480, 800x480,
 * 1024x600, 1280x800, 720x1280 e 800x1280, ma non per 480x800 — il firmware di
 * fabbrica di questa board se l'era costruito. Qui si riusa quasi tutto quello
 * 480x480, che ha la **stessa larghezza**: cambiano solo le misure che dipendono
 * dall'altezza, perche' il resto dello stylesheet e' in percentuale.
 *
 * Con lo stylesheet 480x480 su questo pannello l'interfaccia sta nella parte alta
 * e lascia nero il 40% inferiore (480 su 800): e' cosi' che si sono verificati
 * pannello e touch prima di scrivere questo file.
 */
#pragma once

#include "esp_brookesia.hpp"

/*
 * Launcher: identico a quello 480x480 — la larghezza e' la stessa, quindi icone,
 * testo e indicatori non vanno riscalati — tranne l'altezza della tabella, che
 * da 360 px su 480 di schermo passa a 640 su 800. La barra di navigazione e'
 * nascosta, quindi lo spazio in fondo e' libero; l'altezza effettiva la rifinisce
 * comunque il launcher, che ha `enable_app_launcher_flex_size`.
 */
#define GUITION_480_800_DARK_APP_LAUNCHER_DATA()                                   \
    {                                                                              \
        .main = {                                                                  \
            .y_start = 0,                                                          \
            .size = ESP_BROOKESIA_STYLE_SIZE_RECT_PERCENT(100, 100),               \
        },                                                                         \
        .table = {                                                                 \
            .default_num = 3,                                                      \
            .size = ESP_BROOKESIA_STYLE_SIZE_RECT_W_PERCENT(100, 640),             \
        },                                                                         \
        .indicator = {                                                             \
            .main_size = ESP_BROOKESIA_STYLE_SIZE_RECT_W_PERCENT(100, 80),         \
            .main_layout_column_pad = 10,                                          \
            .main_layout_bottom_offset = 0,                                        \
            .spot_inactive_size = ESP_BROOKESIA_STYLE_SIZE_SQUARE(12),             \
            .spot_active_size = ESP_BROOKESIA_STYLE_SIZE_RECT(30, 12),             \
            .spot_inactive_background_color = ESP_BROOKESIA_STYLE_COLOR(0xC6C6C6), \
            .spot_active_background_color = ESP_BROOKESIA_STYLE_COLOR(0xFFFFFF),   \
        },                                                                         \
        .icon = ESP_BROOKESIA_PHONE_480_480_DARK_APP_LAUNCHER_ICON_DATA(),         \
        .flags = {                                                                 \
            .enable_table_scroll_anim = 0,                                         \
        },                                                                         \
    }

/* L'unico valore davvero vincolante dello stylesheet: la dimensione dello
 * schermo. Con 480x480 qui, Brookesia disegna l'interfaccia nei primi 480 pixel
 * e lascia nero il resto. */
#define GUITION_480_800_DARK_CORE_DATA()                                   \
    {                                                                      \
        .name = "480x800 Guition Dark",                                    \
        .screen_size = ESP_BROOKESIA_STYLE_SIZE_RECT(480, 800),            \
        .home = ESP_BROOKESIA_PHONE_480_480_DARK_CORE_HOME_DATA(),         \
        .manager = ESP_BROOKESIA_PHONE_480_480_DARK_CORE_MANAGER_DATA(),   \
    }

#define GUITION_480_800_DARK_HOME_DATA()                                                                           \
    {                                                                                                              \
        .status_bar = {                                                                                            \
            .data = ESP_BROOKESIA_PHONE_480_480_DARK_STATUS_BAR_DATA(),                                            \
            .visual_mode = ESP_BROOKESIA_STATUS_BAR_VISUAL_MODE_SHOW_FIXED,                                        \
        },                                                                                                         \
        .navigation_bar = {                                                                                        \
            .data = ESP_BROOKESIA_PHONE_480_480_DARK_NAVIGATION_BAR_DATA(),                                        \
            .visual_mode = ESP_BROOKESIA_NAVIGATION_BAR_VISUAL_MODE_HIDE,                                          \
        },                                                                                                         \
        .app_launcher = {                                                                                          \
            .data = GUITION_480_800_DARK_APP_LAUNCHER_DATA(),                                                      \
            .default_image = ESP_BROOKESIA_STYLE_IMAGE(&esp_brookesia_image_middle_app_launcher_default_112_112),   \
        },                                                                                                         \
        .recents_screen = {                                                                                        \
            .data = ESP_BROOKESIA_PHONE_480_480_DARK_RECENTS_SCREEN_DATA(),                                        \
            .status_bar_visual_mode = ESP_BROOKESIA_STATUS_BAR_VISUAL_MODE_HIDE,                                   \
            .navigation_bar_visual_mode = ESP_BROOKESIA_NAVIGATION_BAR_VISUAL_MODE_HIDE,                           \
        },                                                                                                         \
        .flags = {                                                                                                 \
            .enable_status_bar = 1,                                                                                \
            .enable_navigation_bar = 1,                                                                            \
            .enable_app_launcher_flex_size = 1,                                                                    \
            .enable_recents_screen = 1,                                                                            \
            .enable_recents_screen_flex_size = 1,                                                                  \
        },                                                                                                         \
    }

#define GUITION_PHONE_480_800_DARK_STYLESHEET()                      \
    {                                                                \
        .core = GUITION_480_800_DARK_CORE_DATA(),                    \
        .home = GUITION_480_800_DARK_HOME_DATA(),                    \
        .manager = ESP_BROOKESIA_PHONE_480_480_DARK_MANAGER_DATA(),   \
    }
