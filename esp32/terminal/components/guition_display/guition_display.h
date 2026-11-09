/*
 * Supporto hardware per la board Guition JC4880P443C_I_W (ESP32-P4 + ESP32-C6).
 *
 * Perche' questo componente esiste: il BSP di Espressif
 * (`esp32_p4_function_ev_board`) pilota solo i pannelli della EV board ufficiale
 * — 1024x600 (EK79007), 1280x800 (ILI9881C) e HDMI — mentre questa board ha un
 * **4.3" ST7701S da 480x800 verticale**, con la retroilluminazione e il reset su
 * GPIO diversi. Chiamare le `bsp_display_*` qui produce uno schermo nero: il
 * pannello riceve i timing di un altro display. Documentato anche dalle note di
 * bring-up della board (ageiron/jc4880p443c-template, docs/BRINGUP.md).
 *
 * Il resto della catena resta quello del demo: gli handle prodotti qui si
 * passano a `esp_lv_adapter`, e sopra gira ESP-Brookesia senza modifiche.
 *
 * Parametri del pannello (dalla scheda tecnica della board):
 *   480x800 RGB565, MIPI-DSI 2 lane @ 500 Mbps, clock DPI 34 MHz,
 *   LDO3 a 2500 mV per il PHY, reset su GPIO5, retroilluminazione su GPIO23,
 *   touch GT911 su I2C (SDA GPIO7, SCL GPIO8).
 */
#pragma once

#include "esp_err.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_touch.h"

#ifdef __cplusplus
extern "C" {
#endif

#define GUITION_LCD_H_RES        480
#define GUITION_LCD_V_RES        800

/**
 * Accende il pannello e restituisce gli handle per l'adapter LVGL.
 *
 * Fa tutto in ordine: alimenta il PHY MIPI (LDO3), spegne la retroilluminazione
 * finche' il pannello non e' pronto — cosi' non si vede il rumore
 * dell'inizializzazione — apre il bus DSI e il canale comandi DBI, inizializza
 * l'ST7701S e infine riaccende la luce.
 */
esp_err_t guition_display_new(esp_lcd_panel_handle_t *ret_panel,
                              esp_lcd_panel_io_handle_t *ret_io);

/**
 * Retroilluminazione su GPIO23, in PWM (LEDC) come il BSP: serve la luminosita'
 * variabile, perche' l'app Impostazioni ha il suo cursore e la salva in NVS.
 * `percent` va da 0 a 100.
 */
esp_err_t guition_backlight_brightness(int percent);

/** Comodita': accende al 100% o spegne. */
esp_err_t guition_backlight_set(bool on);

/** Touch capacitivo GT911 sul bus I2C della board. */
esp_err_t guition_touch_new(esp_lcd_touch_handle_t *ret_touch);

#ifdef __cplusplus
}
#endif
