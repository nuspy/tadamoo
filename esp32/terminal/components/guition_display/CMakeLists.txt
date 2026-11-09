# Supporto hardware della board Guition JC4880P443C_I_W: pannello ST7701S
# 480x800 e touch GT911. Il driver ST7701 e' quello di Espressif (Apache-2.0),
# preso dal template della board; la variante RGB va compilata anche se questa
# board e' solo MIPI, perche' su ESP32-P4 SOC_LCD_RGB_SUPPORTED e' attivo e il
# file di dispatch del driver la referenzia comunque.
idf_component_register(
    SRCS
        "guition_display.c"
        "esp_lcd_st7701.c"
        "esp_lcd_st7701_mipi.c"
        "esp_lcd_st7701_rgb.c"
    INCLUDE_DIRS "."
    REQUIRES
        driver
        esp_lcd
        esp_driver_i2c
        esp_driver_gpio
        espressif__esp_lcd_touch
        espressif__esp_lcd_touch_gt911
)
