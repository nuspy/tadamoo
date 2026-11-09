/*
 * Reassembly of WebSocket frames delivered in chunks.
 *
 * esp_websocket_client hands a frame to the event handler in pieces of at most
 * its buffer size, each with the frame's payload length and the offset of the
 * piece. A JSON message larger than one piece (a viseme timeline) must be put
 * together before it is parsed. The buffer is supplied by the caller and is
 * never grown: a message that does not fit is dropped whole.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    char *buf;
    size_t cap;
    size_t len;
    bool text_active;     /**< a text message is being collected */
    bool overflow;        /**< the message being collected did not fit */
    bool binary_active;   /**< a binary frame has more pieces to come */
} ws_reasm_t;

typedef enum {
    WSR_NONE = 0,         /**< nothing to deliver yet (or a frame to ignore) */
    WSR_TEXT,             /**< a whole text message: *out, *out_len */
    WSR_BINARY,           /**< a piece of a binary message: *out, *out_len */
    WSR_DROPPED,          /**< a text message was too large and is gone */
} ws_reasm_result_t;

void ws_reasm_init(ws_reasm_t *r, char *buf, size_t cap);

/** Forget a message in progress (new connection). */
void ws_reasm_reset(ws_reasm_t *r);

/**
 * One event of the WebSocket client. `op_code` is 0x0 (continuation), 0x1
 * (text), 0x2 (binary); anything else is ignored. `payload_len` and
 * `payload_offset` are those of the frame, `fin` the final-frame flag.
 * The text result points into `data` or into the buffer and is valid until the
 * next call; it is not NUL terminated.
 */
ws_reasm_result_t ws_reasm_feed(ws_reasm_t *r, int op_code, const char *data, size_t data_len,
                                size_t payload_len, size_t payload_offset, bool fin,
                                const char **out, size_t *out_len);

#ifdef __cplusplus
}
#endif
