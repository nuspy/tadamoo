#include "guition_display.h"

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_touch_gt911.h"
#include "esp_ldo_regulator.h"
#include "esp_log.h"

#include "esp_lcd_st7701.h"

static const char *TAG = "guition";

// Pin e parametri della board (schema JC4880P443C_I_W).
#define PIN_BACKLIGHT           GPIO_NUM_23
#define PIN_LCD_RESET           GPIO_NUM_5
#define TP_I2C_SDA              GPIO_NUM_7
#define TP_I2C_SCL              GPIO_NUM_8
// La retroilluminazione e' in PWM: stessi parametri del BSP (10 bit, 5 kHz), ma
// sul GPIO di questa board. Timer e canale sono dedicati per non litigare con
// eventuali altri utenti del LEDC.
#define BK_LEDC_TIMER           LEDC_TIMER_1
#define BK_LEDC_CHANNEL         LEDC_CHANNEL_1
#define BK_LEDC_DUTY_MAX        1023        // 10 bit
#define MIPI_DSI_PHY_LDO_CHAN   3
#define MIPI_DSI_PHY_LDO_MV     2500
#define LCD_PIXEL_FORMAT        LCD_COLOR_PIXEL_FORMAT_RGB565
#define LCD_BITS_PER_PIXEL      16
#ifdef CONFIG_BSP_LCD_DPI_BUFFER_NUMS
#define LCD_FRAME_BUFFERS       CONFIG_BSP_LCD_DPI_BUFFER_NUMS
#else
#define LCD_FRAME_BUFFERS       1
#endif

static esp_ldo_channel_handle_t s_ldo_mipi_phy = NULL;
// L'I2C e' condiviso: il touch GT911 e il codec audio ES8311 stanno sullo stesso
// bus. Chi arriva secondo deve riusare questo handle, non richiedere di nuovo
// GPIO7/8 (finirebbe con un errore di GPIO gia' occupato).
static i2c_master_bus_handle_t s_i2c_bus = NULL;

static bool s_backlight_ready = false;

static esp_err_t backlight_init(void)
{
    if (s_backlight_ready) {
        return ESP_OK;
    }
    const ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = BK_LEDC_TIMER,
        .freq_hz = 5000,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "timer LEDC");

    const ledc_channel_config_t channel = {
        .gpio_num = PIN_BACKLIGHT,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = BK_LEDC_CHANNEL,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = BK_LEDC_TIMER,
        .duty = 0,
        .hpoint = 0,
    };
    ESP_RETURN_ON_ERROR(ledc_channel_config(&channel), TAG, "canale LEDC");
    s_backlight_ready = true;
    return ESP_OK;
}

esp_err_t guition_backlight_brightness(int percent)
{
    ESP_RETURN_ON_ERROR(backlight_init(), TAG, "retroilluminazione non pronta");
    if (percent > 100) {
        percent = 100;
    } else if (percent < 0) {
        percent = 0;
    }
    const uint32_t duty = (BK_LEDC_DUTY_MAX * percent) / 100;
    ESP_RETURN_ON_ERROR(ledc_set_duty(LEDC_LOW_SPEED_MODE, BK_LEDC_CHANNEL, duty), TAG,
                        "duty LEDC");
    ESP_RETURN_ON_ERROR(ledc_update_duty(LEDC_LOW_SPEED_MODE, BK_LEDC_CHANNEL), TAG,
                        "aggiornamento duty");
    ESP_LOGI(TAG, "retroilluminazione al %d%%", percent);
    return ESP_OK;
}

esp_err_t guition_backlight_set(bool on)
{
    return guition_backlight_brightness(on ? 100 : 0);
}

esp_err_t guition_display_new(esp_lcd_panel_handle_t *ret_panel,
                              esp_lcd_panel_io_handle_t *ret_io)
{
    ESP_RETURN_ON_FALSE(ret_panel && ret_io, ESP_ERR_INVALID_ARG, TAG,
                        "handle di uscita mancanti");

    // 1) Il PHY MIPI va alimentato per primo, altrimenti il bus DSI non parte.
    if (s_ldo_mipi_phy == NULL) {
        esp_ldo_channel_config_t ldo_cfg = {
            .chan_id = MIPI_DSI_PHY_LDO_CHAN,
            .voltage_mv = MIPI_DSI_PHY_LDO_MV,
        };
        ESP_RETURN_ON_ERROR(esp_ldo_acquire_channel(&ldo_cfg, &s_ldo_mipi_phy), TAG,
                            "LDO del PHY MIPI non acquisito");
    }

    // 2) Luce spenta finche' il pannello non e' inizializzato: evita di mostrare
    //    il rumore dei primi frame. Il PWM si configura qui, cosi' e' pronto
    //    quando l'app Impostazioni vorra' cambiare la luminosita'.
    ESP_RETURN_ON_ERROR(backlight_init(), TAG, "retroilluminazione");
    guition_backlight_brightness(0);

    // 3) Bus DSI a 2 lane, 500 Mbps.
    esp_lcd_dsi_bus_handle_t dsi_bus = NULL;
    esp_lcd_dsi_bus_config_t bus_cfg = ST7701_PANEL_BUS_DSI_2CH_CONFIG();
    ESP_RETURN_ON_ERROR(esp_lcd_new_dsi_bus(&bus_cfg, &dsi_bus), TAG, "bus DSI");

    // 4) Canale comandi DBI.
    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_dbi_io_config_t dbi_cfg = ST7701_PANEL_IO_DBI_CONFIG();
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_dbi(dsi_bus, &dbi_cfg, &io), TAG, "IO DBI");

    // 5) Pannello: 480x800 a 34 MHz. Il nome della macro dice "480_360" ma i
    //    timing dentro sono quelli giusti per questo pannello (h 480, v 800).
    esp_lcd_dpi_panel_config_t dpi_cfg = ST7701_480_360_PANEL_60HZ_DPI_CONFIG(LCD_PIXEL_FORMAT);
    /* La macro del driver dichiara un solo frame buffer, ma l'adapter LVGL ne
     * chiede tanti quanti dice il progetto (BSP_LCD_DPI_BUFFER_NUMS, qui 3 per
     * il triple buffering anti-tearing). Se i due numeri non combaciano la
     * richiesta dei buffer fallisce e l'adapter va in crash alla prima scrittura
     * su un puntatore nullo. A 480x800 RGB565 un buffer costa 768 KB, e stanno
     * in PSRAM. */
    dpi_cfg.num_fbs = LCD_FRAME_BUFFERS;
    st7701_vendor_config_t vendor_cfg = {
        .mipi_config = { .dsi_bus = dsi_bus, .dpi_config = &dpi_cfg },
        .flags = { .use_mipi_interface = 1 },
    };
    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = PIN_LCD_RESET,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = LCD_BITS_PER_PIXEL,
        .vendor_config = &vendor_cfg,
    };
    esp_lcd_panel_handle_t panel = NULL;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7701(io, &panel_cfg, &panel), TAG,
                        "pannello ST7701");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(panel), TAG, "reset del pannello");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(panel), TAG, "init del pannello");

    ESP_LOGI(TAG, "ST7701S pronto: %dx%d verticale, DSI 2 lane @500 Mbps, DPI 34 MHz",
             GUITION_LCD_H_RES, GUITION_LCD_V_RES);

    *ret_panel = panel;
    *ret_io = io;
    return ESP_OK;
}

esp_err_t guition_touch_new(esp_lcd_touch_handle_t *ret_touch)
{
    ESP_RETURN_ON_FALSE(ret_touch, ESP_ERR_INVALID_ARG, TAG, "handle di uscita mancante");

    if (s_i2c_bus == NULL) {
        i2c_master_bus_config_t i2c_cfg = {
            .i2c_port = I2C_NUM_0,
            .sda_io_num = TP_I2C_SDA,
            .scl_io_num = TP_I2C_SCL,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .flags = { .enable_internal_pullup = true },
        };
        ESP_RETURN_ON_ERROR(i2c_new_master_bus(&i2c_cfg, &s_i2c_bus), TAG, "bus I2C");
    }

    // I campi vanno impostati nell'ordine di dichiarazione e NON con la macro
    // ESP_LCD_TOUCH_IO_I2C_GT911_CONFIG(): su questa versione dell'header la
    // macro li assegna in ordine sbagliato e il touch non risponde (gotcha
    // documentato nelle note di bring-up della board).
    esp_lcd_panel_io_i2c_config_t tp_io_cfg = {
        .dev_addr = ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS,
        .control_phase_bytes = 1,
        .dc_bit_offset = 0,
        .lcd_cmd_bits = 16,
        .lcd_param_bits = 8,
        .flags = { .disable_control_phase = 1 },
        .scl_speed_hz = 100000,
    };
    esp_lcd_panel_io_handle_t tp_io = NULL;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_i2c(s_i2c_bus, &tp_io_cfg, &tp_io), TAG,
                        "IO I2C del touch");

    esp_lcd_touch_config_t touch_cfg = {
        .x_max = GUITION_LCD_H_RES,
        .y_max = GUITION_LCD_V_RES,
        .rst_gpio_num = GPIO_NUM_NC,
        .int_gpio_num = GPIO_NUM_NC,
        .levels = { .reset = 0, .interrupt = 0 },
        .flags = { .swap_xy = false, .mirror_x = false, .mirror_y = false },
    };
    ESP_RETURN_ON_ERROR(esp_lcd_touch_new_i2c_gt911(tp_io, &touch_cfg, ret_touch), TAG,
                        "touch GT911");

    ESP_LOGI(TAG, "touch GT911 pronto su I2C (SDA %d, SCL %d)", TP_I2C_SDA, TP_I2C_SCL);
    return ESP_OK;
}
