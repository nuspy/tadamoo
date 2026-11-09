/*
 * The viseme timeline of the gateway (docs/voice/gateway.md, Render modes) and
 * the clock that plays it.
 *
 * A `visemes` message carries, for one audio segment, a list of
 * {t, v, dur} in milliseconds from the start of that segment. The mouth is
 * drawn from the position of the audio being played, not from the arrival time
 * of the message. No ESP-IDF headers: the caller passes the time.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VT_MAX_ITEMS     512      /**< items kept per segment; the rest are dropped */
#define VT_MAX_SEGMENTS  8        /**< timelines queued ahead of the playback */
#define VT_MAX_TIME_MS   3600000u /**< a time beyond this is not a timeline */
#define VT_RAMP_MS       60       /**< cross-fade between two shapes */

typedef enum {
    VT_SIL = 0, VT_PP, VT_FF, VT_TH, VT_DD, VT_KK, VT_CH, VT_SS,
    VT_NN, VT_RR, VT_AA, VT_E, VT_I, VT_O, VT_U,
    VT_COUNT
} vt_viseme_t;

typedef struct {
    uint32_t t_ms;
    uint16_t dur_ms;     /**< 0: until the next item */
    uint8_t v;           /**< vt_viseme_t */
} vt_item_t;

typedef struct {
    int32_t segment;
    uint32_t pcm_bytes;
    uint32_t duration_ms;
    uint16_t n;
    bool truncated;      /**< more than VT_MAX_ITEMS arrived */
    vt_item_t items[VT_MAX_ITEMS];
} vt_segment_t;

/** Mouth shape in per mille: opening in mouth widths, width against the neutral one. */
typedef struct {
    int16_t open_pm;
    int16_t wide_pm;
} vt_mouth_t;

enum {
    VT_OK = 0,
    VT_ERR_JSON = -1,    /**< not JSON, cut short, or nested too deep */
    VT_ERR_TYPE = -2,    /**< a message that is not "visemes" */
    VT_ERR_FIELD = -3,   /**< no "visemes" array */
};

/** Parses one `visemes` message (not NUL terminated). Never reads past `len`. */
int vt_parse_message(const char *json, size_t len, vt_segment_t *out);

/** The "type" of a message (top-level key, NUL terminated in `out`); false when there is none. */
bool vt_peek_type(const char *json, size_t len, char *out, size_t cap);

/** The shape for a viseme name; an unknown name is `sil`. */
vt_viseme_t vt_viseme_from_name(const char *name);
vt_mouth_t vt_mouth_of(vt_viseme_t v);

/** The shape at `t_ms` from the start of `seg`, cross-faded between items. */
vt_mouth_t vt_mouth_at(const vt_segment_t *seg, uint32_t t_ms);

typedef struct {
    vt_segment_t seg[VT_MAX_SEGMENTS];
    uint64_t start_us[VT_MAX_SEGMENTS];
    uint64_t len_us[VT_MAX_SEGMENTS];
    uint8_t head;
    uint8_t count;
    uint32_t rate;               /**< sample rate of the reply, 0 before audio_start */
    uint64_t received_bytes;     /**< PCM of this reply received so far */
    uint64_t pos_us;             /**< playback position in the reply */
    uint64_t last_us;            /**< time of the last advance */
    bool ended;                  /**< `audio_end` seen: the position may run past the received audio */
    bool external;               /**< the position comes from the speaker */
    uint64_t external_us;
    uint32_t dropped;            /**< timelines that did not fit the queue */
    uint32_t refused;            /**< timelines refused (no reply open) */
} vt_player_t;

/** Forget everything: the mouth goes to `sil`. */
void vt_player_reset(vt_player_t *p);

/** A new reply (`audio_start`). An implausible rate is replaced by 24000. */
void vt_player_begin_reply(vt_player_t *p, uint32_t sample_rate);

/**
 * The timeline of the next segment. It starts where the PCM received so far
 * ends, which is where the speaker will be when it reaches this segment.
 */
bool vt_player_add_segment(vt_player_t *p, const vt_segment_t *seg);

/**
 * PCM received. Without a speaker the position is a virtual one that runs at
 * the audio rate while there is received audio not yet "played", and waits
 * when there is none.
 */
void vt_player_on_pcm(vt_player_t *p, size_t nbytes, uint64_t now_us);

/** No more PCM will come (`audio_end`): what is queued plays out, then the mouth closes. */
void vt_player_end_reply(vt_player_t *p);

/** The real playback position (samples written to the codec): replaces the virtual one. */
void vt_player_set_played_us(vt_player_t *p, uint64_t played_us);

/** Advances to `now_us` and returns the mouth to draw; `*active` tells whether a timeline is running. */
vt_mouth_t vt_player_sample(vt_player_t *p, uint64_t now_us, bool *active);

/** Checks the parser, the shapes and the clock on a built-in timeline. 0 when all is well. */
int vt_selftest(vt_player_t *scratch);

#ifdef __cplusplus
}
#endif
