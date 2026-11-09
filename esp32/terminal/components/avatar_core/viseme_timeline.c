#include "viseme_timeline.h"

#include <string.h>

/* ---------------------------------------------------------------- shapes */

static const char *const NAMES[VT_COUNT] = {
    "sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR", "aa", "E", "I", "O", "U",
};

/* open (mouth widths) and wide (against neutral), per mille: docs/voice/avatar.md */
static const vt_mouth_t SHAPES[VT_COUNT] = {
    {0, 1000},   {0, 960},    {70, 1000},  {100, 1000}, {140, 1000},
    {180, 980},  {140, 780},  {70, 1080},  {100, 1000}, {140, 850},
    {420, 1040}, {260, 1080}, {120, 1140}, {300, 700},  {180, 560},
};

vt_viseme_t vt_viseme_from_name(const char *name)
{
    if (name != NULL) {
        for (int i = 0; i < VT_COUNT; i++) {
            if (strcmp(name, NAMES[i]) == 0) {
                return (vt_viseme_t)i;
            }
        }
    }
    return VT_SIL;
}

vt_mouth_t vt_mouth_of(vt_viseme_t v)
{
    return SHAPES[((unsigned)v < VT_COUNT) ? v : VT_SIL];
}

static int16_t lerp16(int16_t a, int16_t b, int32_t w_pm)
{
    return (int16_t)(a + (int32_t)(b - a) * w_pm / 1000);
}

static vt_mouth_t lerp_mouth(vt_mouth_t a, vt_mouth_t b, int32_t w_pm)
{
    if (w_pm <= 0) {
        return a;
    }
    if (w_pm >= 1000) {
        return b;
    }
    vt_mouth_t m = {lerp16(a.open_pm, b.open_pm, w_pm), lerp16(a.wide_pm, b.wide_pm, w_pm)};
    return m;
}

/* ---------------------------------------------------------------- parser */

#define JSON_MAX_DEPTH 16

typedef struct {
    const char *p;
    const char *end;
} scan_t;

static void skip_ws(scan_t *s)
{
    while (s->p < s->end && (*s->p == ' ' || *s->p == '\t' || *s->p == '\n' || *s->p == '\r')) {
        s->p++;
    }
}

static bool accept(scan_t *s, char c)
{
    skip_ws(s);
    if (s->p < s->end && *s->p == c) {
        s->p++;
        return true;
    }
    return false;
}

static bool is_hex(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

/* A string; copied into dst (NUL terminated, cut to cap) when dst is not NULL. */
static bool read_string(scan_t *s, char *dst, size_t cap)
{
    size_t n = 0;
    if (!accept(s, '"')) {
        return false;
    }
    while (s->p < s->end) {
        char c = *s->p++;
        if (c == '"') {
            if (dst != NULL && cap > 0) {
                dst[n] = '\0';
            }
            return true;
        }
        if (c == '\\') {
            if (s->p >= s->end) {
                return false;
            }
            char e = *s->p++;
            if (e == 'u') {
                for (int i = 0; i < 4; i++) {
                    if (s->p >= s->end || !is_hex(*s->p)) {
                        return false;
                    }
                    s->p++;
                }
            }
            c = '?';
        }
        if (dst != NULL && n + 1 < cap) {
            dst[n++] = c;
        }
    }
    return false;
}

/* An integer; a fraction or an exponent is read and cut off. Saturates. */
static bool read_int(scan_t *s, int64_t *v)
{
    skip_ws(s);
    bool neg = false;
    if (s->p < s->end && *s->p == '-') {
        neg = true;
        s->p++;
    }
    if (s->p >= s->end || *s->p < '0' || *s->p > '9') {
        return false;
    }
    int64_t acc = 0;
    while (s->p < s->end && *s->p >= '0' && *s->p <= '9') {
        if (acc < ((int64_t)1 << 40)) {
            acc = acc * 10 + (*s->p - '0');
        }
        s->p++;
    }
    while (s->p < s->end && (*s->p == '.' || *s->p == 'e' || *s->p == 'E' || *s->p == '+' ||
                             *s->p == '-' || (*s->p >= '0' && *s->p <= '9'))) {
        s->p++;
    }
    *v = neg ? -acc : acc;
    return true;
}

static bool match_literal(scan_t *s, const char *lit)
{
    const size_t n = strlen(lit);
    if ((size_t)(s->end - s->p) < n || memcmp(s->p, lit, n) != 0) {
        return false;
    }
    s->p += n;
    return true;
}

static bool skip_value(scan_t *s, int depth)
{
    if (depth > JSON_MAX_DEPTH) {
        return false;
    }
    skip_ws(s);
    if (s->p >= s->end) {
        return false;
    }
    const char c = *s->p;
    if (c == '"') {
        return read_string(s, NULL, 0);
    }
    if (c == '{' || c == '[') {
        const char close = (c == '{') ? '}' : ']';
        s->p++;
        if (accept(s, close)) {
            return true;
        }
        for (;;) {
            if (c == '{') {
                if (!read_string(s, NULL, 0) || !accept(s, ':')) {
                    return false;
                }
            }
            if (!skip_value(s, depth + 1)) {
                return false;
            }
            if (accept(s, close)) {
                return true;
            }
            if (!accept(s, ',')) {
                return false;
            }
        }
    }
    if (c == 't') {
        return match_literal(s, "true");
    }
    if (c == 'f') {
        return match_literal(s, "false");
    }
    if (c == 'n') {
        return match_literal(s, "null");
    }
    int64_t ignored;
    return read_int(s, &ignored);
}

typedef struct {
    bool have_t, have_v, have_dur;
    int64_t t, dur;
    char v[8];
} item_fields_t;

static bool parse_item(scan_t *s, item_fields_t *f)
{
    memset(f, 0, sizeof(*f));
    if (!accept(s, '{')) {
        return false;
    }
    if (accept(s, '}')) {
        return true;
    }
    for (;;) {
        char key[8];
        if (!read_string(s, key, sizeof(key)) || !accept(s, ':')) {
            return false;
        }
        skip_ws(s);
        if (strcmp(key, "t") == 0 && s->p < s->end && *s->p != '"') {
            f->have_t = read_int(s, &f->t);
            if (!f->have_t) {
                return false;
            }
        } else if (strcmp(key, "dur") == 0 && s->p < s->end && *s->p != '"') {
            f->have_dur = read_int(s, &f->dur);
            if (!f->have_dur) {
                return false;
            }
        } else if (strcmp(key, "v") == 0 && s->p < s->end && *s->p == '"') {
            f->have_v = read_string(s, f->v, sizeof(f->v));
            if (!f->have_v) {
                return false;
            }
        } else if (!skip_value(s, 2)) {
            return false;
        }
        if (accept(s, '}')) {
            return true;
        }
        if (!accept(s, ',')) {
            return false;
        }
    }
}

static bool parse_items(scan_t *s, vt_segment_t *out)
{
    if (!accept(s, '[')) {
        return false;
    }
    if (accept(s, ']')) {
        return true;
    }
    uint32_t last_t = 0;
    for (;;) {
        item_fields_t f;
        if (!parse_item(s, &f)) {
            return false;
        }
        if (f.have_t && f.t >= 0 && f.t <= (int64_t)VT_MAX_TIME_MS) {
            if (out->n < VT_MAX_ITEMS) {
                vt_item_t *it = &out->items[out->n];
                it->t_ms = (uint32_t)f.t < last_t ? last_t : (uint32_t)f.t;
                it->dur_ms = (f.have_dur && f.dur > 0) ? (uint16_t)(f.dur > 60000 ? 60000 : f.dur) : 0;
                it->v = (uint8_t)(f.have_v ? vt_viseme_from_name(f.v) : VT_SIL);
                last_t = it->t_ms;
                out->n++;
            } else {
                out->truncated = true;
            }
        }
        if (accept(s, ']')) {
            return true;
        }
        if (!accept(s, ',')) {
            return false;
        }
    }
}

bool vt_peek_type(const char *json, size_t len, char *out, size_t cap)
{
    if (json == NULL || out == NULL || cap == 0) {
        return false;
    }
    scan_t s = {json, json + len};
    if (!accept(&s, '{') || accept(&s, '}')) {
        return false;
    }
    for (;;) {
        char key[8];
        if (!read_string(&s, key, sizeof(key)) || !accept(&s, ':')) {
            return false;
        }
        skip_ws(&s);
        if (strcmp(key, "type") == 0 && s.p < s.end && *s.p == '"') {
            return read_string(&s, out, cap);
        }
        if (!skip_value(&s, 1) || !accept(&s, ',')) {
            return false;
        }
    }
}

int vt_parse_message(const char *json, size_t len, vt_segment_t *out)
{
    if (json == NULL || out == NULL) {
        return VT_ERR_JSON;
    }
    out->segment = 0;
    out->pcm_bytes = 0;
    out->duration_ms = 0;
    out->n = 0;
    out->truncated = false;

    scan_t s = {json, json + len};
    bool have_type = false, is_visemes = false, have_items = false;

    if (!accept(&s, '{')) {
        return VT_ERR_JSON;
    }
    if (!accept(&s, '}')) {
        for (;;) {
            char key[16];
            if (!read_string(&s, key, sizeof(key)) || !accept(&s, ':')) {
                return VT_ERR_JSON;
            }
            skip_ws(&s);
            const bool is_string = (s.p < s.end && *s.p == '"');
            if (strcmp(key, "type") == 0 && is_string) {
                char type[16];
                if (!read_string(&s, type, sizeof(type))) {
                    return VT_ERR_JSON;
                }
                have_type = true;
                is_visemes = (strcmp(type, "visemes") == 0);
            } else if (strcmp(key, "visemes") == 0 && s.p < s.end && *s.p == '[') {
                out->n = 0;
                out->truncated = false;
                if (!parse_items(&s, out)) {
                    return VT_ERR_JSON;
                }
                have_items = true;
            } else if ((strcmp(key, "segment") == 0 || strcmp(key, "pcm_bytes") == 0 ||
                        strcmp(key, "duration_ms") == 0) && !is_string) {
                int64_t v;
                if (!read_int(&s, &v)) {
                    if (!skip_value(&s, 1)) {
                        return VT_ERR_JSON;
                    }
                } else if (v >= 0 && v <= (int64_t)0x7fffffff) {
                    if (key[0] == 's') {
                        out->segment = (int32_t)v;
                    } else if (key[0] == 'p') {
                        out->pcm_bytes = (uint32_t)v;
                    } else {
                        out->duration_ms = (uint32_t)v;
                    }
                }
            } else if (!skip_value(&s, 1)) {
                return VT_ERR_JSON;
            }
            if (accept(&s, '}')) {
                break;
            }
            if (!accept(&s, ',')) {
                return VT_ERR_JSON;
            }
        }
    }
    if (!have_type || !is_visemes) {
        return VT_ERR_TYPE;
    }
    return have_items ? VT_OK : VT_ERR_FIELD;
}

/* ---------------------------------------------------------------- sampling */

static uint32_t item_len_ms(const vt_segment_t *seg, uint16_t i)
{
    const vt_item_t *it = &seg->items[i];
    if (it->dur_ms != 0) {
        return it->dur_ms;
    }
    if (i + 1u < seg->n) {
        return seg->items[i + 1].t_ms - it->t_ms;
    }
    return 100;
}

vt_mouth_t vt_mouth_at(const vt_segment_t *seg, uint32_t t_ms)
{
    const vt_mouth_t sil = SHAPES[VT_SIL];
    if (seg == NULL || seg->n == 0 || t_ms < seg->items[0].t_ms) {
        return sil;
    }
    /* the last item started at or before t_ms */
    uint16_t lo = 0, hi = (uint16_t)(seg->n - 1);
    while (lo < hi) {
        const uint16_t mid = (uint16_t)((lo + hi + 1) / 2);
        if (seg->items[mid].t_ms <= t_ms) {
            lo = mid;
        } else {
            hi = (uint16_t)(mid - 1);
        }
    }
    const vt_item_t *it = &seg->items[lo];
    const uint32_t len = item_len_ms(seg, lo);
    const vt_mouth_t cur = vt_mouth_of((vt_viseme_t)it->v);

    uint32_t ramp = VT_RAMP_MS;
    if (len < 2 * ramp) {
        ramp = len / 2;
    }

    const uint32_t elapsed = t_ms - it->t_ms;
    if (elapsed >= len) {
        /* a gap, or the end: the mouth relaxes to `sil` */
        const uint32_t gone = elapsed - len;
        const uint32_t r = ramp ? ramp : 1;
        return lerp_mouth(cur, sil, (int32_t)(gone >= r ? 1000 : gone * 1000 / r));
    }

    vt_mouth_t prev = sil;
    if (lo > 0) {
        const vt_item_t *pi = &seg->items[lo - 1];
        const uint32_t prev_end = pi->t_ms + item_len_ms(seg, (uint16_t)(lo - 1));
        if (prev_end + 5 >= it->t_ms) {
            prev = vt_mouth_of((vt_viseme_t)pi->v);
        }
    }
    if (ramp == 0 || elapsed >= ramp) {
        return cur;
    }
    return lerp_mouth(prev, cur, (int32_t)(elapsed * 1000 / ramp));
}

/* ---------------------------------------------------------------- player */

#define DEFAULT_RATE 24000u

void vt_player_reset(vt_player_t *p)
{
    memset(p, 0, sizeof(*p));
}

void vt_player_begin_reply(vt_player_t *p, uint32_t sample_rate)
{
    vt_player_reset(p);
    p->rate = (sample_rate >= 4000 && sample_rate <= 96000) ? sample_rate : DEFAULT_RATE;
}

static uint64_t bytes_to_us(const vt_player_t *p, uint64_t bytes)
{
    return p->rate ? bytes * 500000ull / p->rate : 0;
}

static void advance(vt_player_t *p, uint64_t now_us)
{
    if (p->external) {
        p->pos_us = p->external_us;
        return;
    }
    if (p->received_bytes == 0 && !p->ended) {
        return;
    }
    const uint64_t dt = (now_us > p->last_us) ? now_us - p->last_us : 0;
    const uint64_t limit = p->ended ? UINT64_MAX : bytes_to_us(p, p->received_bytes);
    p->pos_us = (p->pos_us + dt < limit) ? p->pos_us + dt : limit;
    p->last_us = now_us;
}

static void prune(vt_player_t *p)
{
    while (p->count > 0) {
        const uint8_t h = p->head;
        if (p->pos_us <= p->start_us[h] + p->len_us[h] + (uint64_t)VT_RAMP_MS * 1000) {
            break;
        }
        p->head = (uint8_t)((h + 1) % VT_MAX_SEGMENTS);
        p->count--;
    }
}

bool vt_player_add_segment(vt_player_t *p, const vt_segment_t *seg)
{
    if (p->rate == 0 || seg == NULL) {
        p->refused++;
        return false;
    }
    prune(p);
    if (p->count >= VT_MAX_SEGMENTS) {
        p->dropped++;
        return false;
    }
    const uint8_t slot = (uint8_t)((p->head + p->count) % VT_MAX_SEGMENTS);
    /* only the used part of the segment is copied */
    const uint16_t n = seg->n < VT_MAX_ITEMS ? seg->n : VT_MAX_ITEMS;
    const size_t used = offsetof(vt_segment_t, items) + (size_t)n * sizeof(vt_item_t);
    memcpy(&p->seg[slot], seg, used);
    p->seg[slot].n = n;
    p->start_us[slot] = bytes_to_us(p, p->received_bytes);
    p->len_us[slot] = seg->pcm_bytes ? bytes_to_us(p, seg->pcm_bytes) : (uint64_t)seg->duration_ms * 1000;
    p->count++;
    return true;
}

void vt_player_on_pcm(vt_player_t *p, size_t nbytes, uint64_t now_us)
{
    if (p->rate == 0) {
        return;
    }
    if (p->received_bytes == 0) {
        p->last_us = now_us;
    } else {
        advance(p, now_us);   /* time spent with nothing to play is not played */
    }
    p->received_bytes += nbytes;
}

void vt_player_end_reply(vt_player_t *p)
{
    p->ended = true;
}

void vt_player_set_played_us(vt_player_t *p, uint64_t played_us)
{
    p->external = true;
    p->external_us = played_us;
}

vt_mouth_t vt_player_sample(vt_player_t *p, uint64_t now_us, bool *active)
{
    if (active != NULL) {
        *active = false;
    }
    if (p->count == 0) {
        return SHAPES[VT_SIL];
    }
    advance(p, now_us);
    prune(p);

    int found = -1;
    for (uint8_t i = 0; i < p->count; i++) {
        const uint8_t slot = (uint8_t)((p->head + i) % VT_MAX_SEGMENTS);
        if (p->start_us[slot] <= p->pos_us) {
            found = slot;
        }
    }
    if (found < 0) {
        return SHAPES[VT_SIL];
    }
    if (active != NULL) {
        *active = true;
    }
    const uint64_t t_ms = (p->pos_us - p->start_us[found]) / 1000;
    return vt_mouth_at(&p->seg[found], t_ms > 0xffffffffu ? 0xffffffffu : (uint32_t)t_ms);
}

/* ---------------------------------------------------------------- selftest */

int vt_selftest(vt_player_t *scratch)
{
    static const char MSG[] =
        "{\"type\":\"visemes\",\"text\":\"a \\\"q\\\" [x] {y} \\u00e8\",\"lang\":\"it\",\"duration_ms\":1000,"
        "\"visemes\":[{\"t\":0,\"v\":\"sil\",\"dur\":100},{\"t\":100,\"v\":\"aa\",\"dur\":400},"
        "{\"t\":500,\"v\":\"PP\",\"dur\":500}],\"segment\":3,\"pcm_bytes\":48000}";
    if (scratch == NULL) {
        return 1;
    }
    vt_segment_t *seg = &scratch->seg[0];
    if (vt_parse_message(MSG, sizeof(MSG) - 1, seg) != VT_OK || seg->n != 3 || seg->segment != 3 ||
        seg->pcm_bytes != 48000 || seg->items[1].v != VT_AA) {
        return 2;
    }
    if (vt_mouth_at(seg, 300).open_pm != SHAPES[VT_AA].open_pm || vt_mouth_at(seg, 800).open_pm != 0) {
        return 3;
    }
    const int16_t mid = vt_mouth_at(seg, 100 + VT_RAMP_MS / 2).open_pm;
    if (mid <= 0 || mid >= SHAPES[VT_AA].open_pm) {
        return 4;
    }
    /* the player copies the segment into its own queue: parse into a local copy first */
    vt_segment_t copy;
    memcpy(&copy, seg, offsetof(vt_segment_t, items) + (size_t)seg->n * sizeof(vt_item_t));
    vt_player_begin_reply(scratch, 24000);
    if (!vt_player_add_segment(scratch, &copy)) {
        return 5;
    }
    vt_player_on_pcm(scratch, 48000, 1000000);
    bool active = false;
    const vt_mouth_t m = vt_player_sample(scratch, 1000000 + 300000, &active);
    const int ok = active && m.open_pm == SHAPES[VT_AA].open_pm;
    vt_player_reset(scratch);
    return ok ? 0 : 6;
}
