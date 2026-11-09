# Client del voice gateway: WebSocket + JSON, nessuna logica di voce.
idf_component_register(
    SRCS "gateway_client.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_websocket_client
        json
        esp_event
)
