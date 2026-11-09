#include "ws_reasm.h"

#include <string.h>

void ws_reasm_init(ws_reasm_t *r, char *buf, size_t cap)
{
    memset(r, 0, sizeof(*r));
    r->buf = buf;
    r->cap = cap;
}

void ws_reasm_reset(ws_reasm_t *r)
{
    r->len = 0;
    r->text_active = false;
    r->overflow = false;
    r->binary_active = false;
}

ws_reasm_result_t ws_reasm_feed(ws_reasm_t *r, int op_code, const char *data, size_t data_len,
                                size_t payload_len, size_t payload_offset, bool fin,
                                const char **out, size_t *out_len)
{
    if (r == NULL || out == NULL || out_len == NULL || (data == NULL && data_len > 0)) {
        return WSR_NONE;
    }
    const bool frame_done = (payload_offset + data_len >= payload_len);

    if (op_code == 0x2 || (op_code == 0x0 && r->binary_active)) {
        r->binary_active = !(frame_done && fin);
        if (op_code == 0x2) {
            r->text_active = false;
        }
        *out = data;
        *out_len = data_len;
        return WSR_BINARY;
    }

    if (op_code == 0x1) {
        if (payload_offset == 0) {
            r->len = 0;
            r->text_active = true;
            r->overflow = false;
            r->binary_active = false;
            if (data_len == payload_len && fin) {
                r->text_active = false;
                *out = data;
                *out_len = data_len;
                return WSR_TEXT;
            }
        } else if (!r->text_active) {
            return WSR_NONE;
        }
    } else if (op_code == 0x0) {
        if (!r->text_active) {
            return WSR_NONE;
        }
    } else {
        return WSR_NONE;
    }

    if (!r->overflow) {
        if (r->buf == NULL || data_len > r->cap - r->len) {
            r->overflow = true;
        } else {
            memcpy(r->buf + r->len, data, data_len);
            r->len += data_len;
        }
    }

    if (frame_done && fin) {
        r->text_active = false;
        if (r->overflow) {
            r->overflow = false;
            r->len = 0;
            return WSR_DROPPED;
        }
        *out = r->buf;
        *out_len = r->len;
        return WSR_TEXT;
    }
    return WSR_NONE;
}
