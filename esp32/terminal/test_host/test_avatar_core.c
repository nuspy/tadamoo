/*
 * Host test of the pure parts of the avatar: the viseme timeline (parse, shapes,
 * playback clock) and the WebSocket reassembly. No ESP-IDF, no LVGL.
 *
 *   clang -std=c11 -Wall -Wextra -Werror -I../components/avatar_core/include \
 *       test_avatar_core.c ../components/avatar_core/viseme_timeline.c \
 *       ../components/avatar_core/ws_reasm.c -o test_avatar_core && ./test_avatar_core
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "viseme_timeline.h"
#include "ws_reasm.h"

static int g_checks, g_failed;

#define CHECK(cond)                                                                   \
    do {                                                                              \
        g_checks++;                                                                   \
        if (!(cond)) {                                                                \
            g_failed++;                                                               \
            printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);                    \
        }                                                                             \
    } while (0)

static int parse(const char *json, vt_segment_t *seg)
{
    return vt_parse_message(json, strlen(json), seg);
}

static const char *MSG =
    "{\"type\": \"visemes\", \"text\": \"Ciao \\\"a\\\" [tutti] {x} \\u00e8 \\\\\", \"lang\": \"it\", "
    "\"duration_ms\": 1000, \"visemes\": [{\"t\": 0, \"v\": \"sil\", \"dur\": 100}, "
    "{\"t\": 100, \"v\": \"aa\", \"dur\": 400}, {\"t\": 500, \"v\": \"PP\", \"dur\": 500}], "
    "\"segment\": 7, \"pcm_bytes\": 48000}";

static void test_parse(void)
{
    vt_segment_t *s = calloc(1, sizeof(*s));
    CHECK(parse(MSG, s) == VT_OK);
    CHECK(s->n == 3 && s->segment == 7 && s->pcm_bytes == 48000 && s->duration_ms == 1000);
    CHECK(s->items[1].t_ms == 100 && s->items[1].dur_ms == 400 && s->items[1].v == VT_AA);
    CHECK(s->items[2].v == VT_PP && !s->truncated);

    /* every prefix of a valid message is refused or ends the object: never read past len */
    const size_t n = strlen(MSG);
    int refused = 0;
    for (size_t cut = 0; cut < n; cut++) {
        if (vt_parse_message(MSG, cut, s) != VT_OK) {
            refused++;
        }
    }
    CHECK(refused == (int)n);

    CHECK(parse("not json", s) == VT_ERR_JSON);
    CHECK(parse("[]", s) == VT_ERR_JSON);
    CHECK(parse("{\"type\":\"reply\",\"chunk\":\"x\"}", s) == VT_ERR_TYPE);
    CHECK(parse("{\"visemes\":[]}", s) == VT_ERR_TYPE);
    CHECK(parse("{\"type\":\"visemes\"}", s) == VT_ERR_FIELD);
    CHECK(parse("{\"type\":\"visemes\",\"visemes\":[]}", s) == VT_OK && s->n == 0);
    CHECK(vt_parse_message(NULL, 0, s) == VT_ERR_JSON);

    /* unknown name -> sil, negative time and missing t dropped, order repaired, fraction cut, extras skipped */
    CHECK(parse("{\"type\":\"visemes\",\"x\":{\"a\":[1,2,{\"b\":null}],\"c\":true},\"visemes\":["
                "{\"t\":10,\"v\":\"zz\"},{\"t\":-5,\"v\":\"aa\"},{\"v\":\"aa\"},{\"t\":5,\"v\":\"O\",\"dur\":2.9e0,\"k\":1},"
                "{\"t\":99.7,\"v\":\"U\",\"dur\":\"x\"}]}", s) == VT_OK);
    CHECK(s->n == 3);
    CHECK(s->items[0].v == VT_SIL && s->items[0].t_ms == 10);
    CHECK(s->items[1].v == VT_O && s->items[1].t_ms == 10 && s->items[1].dur_ms == 2);
    CHECK(s->items[2].v == VT_U && s->items[2].t_ms == 99 && s->items[2].dur_ms == 0);

    /* the type is found without parsing the rest */
    char ty[16];
    const char *typed = "{\"a\":[1,{\"type\":\"no\"}],\"type\":\"ready\"}";
    CHECK(vt_peek_type(MSG, strlen(MSG), ty, sizeof(ty)) && strcmp(ty, "visemes") == 0);
    CHECK(vt_peek_type(typed, strlen(typed), ty, sizeof(ty)) && strcmp(ty, "ready") == 0);
    CHECK(!vt_peek_type(typed, strlen(typed) - 4, ty, sizeof(ty)));
    CHECK(!vt_peek_type("{}", 2, ty, sizeof(ty)) && !vt_peek_type("{\"type\":5}", 10, ty, sizeof(ty)));
    CHECK(!vt_peek_type("{\"type\":\"vis", 12, ty, sizeof(ty)) && !vt_peek_type(NULL, 0, ty, sizeof(ty)));

    /* nesting beyond the limit is refused, not recursed into */
    char deep[256];
    size_t dn = (size_t)snprintf(deep, sizeof(deep), "{\"type\":\"visemes\",\"x\":");
    memset(deep + dn, '[', 60);
    deep[dn + 60] = '\0';
    CHECK(parse(deep, s) == VT_ERR_JSON);

    /* more items than the cap: the cap is kept, the flag is set */
    size_t cap = 64 + (size_t)(VT_MAX_ITEMS + 50) * 40;
    char *big = malloc(cap);
    size_t pos = (size_t)sprintf(big, "{\"type\":\"visemes\",\"visemes\":[");
    for (int i = 0; i < VT_MAX_ITEMS + 50; i++) {
        pos += (size_t)sprintf(big + pos, "%s{\"t\":%d,\"v\":\"aa\",\"dur\":10}", i ? "," : "", i * 10);
    }
    memcpy(big + pos, "]}", 3);
    CHECK(parse(big, s) == VT_OK && s->n == VT_MAX_ITEMS && s->truncated);
    free(big);
    free(s);
}

static void test_shapes(void)
{
    CHECK(vt_viseme_from_name("aa") == VT_AA && vt_viseme_from_name("U") == VT_U);
    CHECK(vt_viseme_from_name("AA") == VT_SIL && vt_viseme_from_name(NULL) == VT_SIL);
    CHECK(vt_mouth_of(VT_AA).open_pm == 420 && vt_mouth_of(VT_PP).open_pm == 0);
    CHECK(vt_mouth_of(VT_U).wide_pm == 560 && vt_mouth_of(VT_SIL).wide_pm == 1000);

    vt_segment_t *s = calloc(1, sizeof(*s));
    CHECK(parse(MSG, s) == VT_OK);
    CHECK(vt_mouth_at(s, 0).open_pm == 0);
    CHECK(vt_mouth_at(s, 300).open_pm == 420);                 /* plateau of aa */
    CHECK(vt_mouth_at(s, 800).open_pm == 0);                   /* PP */
    int16_t mid = vt_mouth_at(s, 100 + VT_RAMP_MS / 2).open_pm;
    CHECK(mid > 150 && mid < 270);                             /* halfway sil -> aa */
    int16_t prev = -1;
    for (uint32_t t = 100; t <= 100 + VT_RAMP_MS; t += 5) {    /* monotone rise */
        int16_t o = vt_mouth_at(s, t).open_pm;
        CHECK(o >= prev);
        prev = o;
    }
    CHECK(vt_mouth_at(s, 1000 + 10).open_pm == 0);             /* after the end: sil */
    CHECK(vt_mouth_at(NULL, 5).open_pm == 0);

    /* an item without dur lasts until the next one; a gap relaxes to sil; before the first item is sil */
    CHECK(parse("{\"type\":\"visemes\",\"visemes\":[{\"t\":50,\"v\":\"O\"},{\"t\":400,\"v\":\"aa\",\"dur\":100}]}", s) == VT_OK);
    CHECK(vt_mouth_at(s, 10).open_pm == 0);
    CHECK(vt_mouth_at(s, 200).open_pm == 300);
    CHECK(vt_mouth_at(s, 399).open_pm == 300);
    CHECK(parse("{\"type\":\"visemes\",\"visemes\":[{\"t\":0,\"v\":\"aa\",\"dur\":100},{\"t\":400,\"v\":\"aa\",\"dur\":100}]}", s) == VT_OK);
    CHECK(vt_mouth_at(s, 130).open_pm > 0 && vt_mouth_at(s, 130).open_pm < 420);   /* relaxing */
    CHECK(vt_mouth_at(s, 300).open_pm == 0);                                        /* gap */
    CHECK(vt_mouth_at(s, 450).open_pm == 420);
    free(s);
}

static vt_segment_t *make_segment(int segment, uint32_t pcm_bytes, const char *v, uint32_t dur_ms)
{
    vt_segment_t *s = calloc(1, sizeof(*s));
    s->segment = segment;
    s->pcm_bytes = pcm_bytes;
    s->duration_ms = dur_ms;
    s->n = 1;
    s->items[0].t_ms = 0;
    s->items[0].dur_ms = (uint16_t)dur_ms;
    s->items[0].v = (uint8_t)vt_viseme_from_name(v);
    return s;
}

static void test_player(void)
{
    vt_player_t *p = calloc(1, sizeof(*p));
    bool active;
    uint64_t t0 = 5000000;   /* arbitrary wall clock */

    CHECK(vt_selftest(p) == 0);

    /* before audio_start a timeline is refused */
    vt_segment_t *a = make_segment(1, 48000, "aa", 1000);
    CHECK(!vt_player_add_segment(p, a) && p->refused == 1);

    /* 24000 Hz: 48000 bytes = 1 s. Segment 1 = aa for 1 s, segment 2 = O for 1 s. */
    vt_player_begin_reply(p, 24000);
    CHECK(vt_player_add_segment(p, a));
    vt_player_on_pcm(p, 48000, t0);
    vt_segment_t *b = make_segment(2, 48000, "O", 1000);
    CHECK(vt_player_add_segment(p, b));   /* starts where the PCM so far ends: 1 s */
    CHECK(p->start_us[1] == 1000000);
    vt_player_on_pcm(p, 48000, t0 + 10000);

    vt_mouth_t m = vt_player_sample(p, t0 + 500000, &active);
    CHECK(active && m.open_pm == 420);
    m = vt_player_sample(p, t0 + 1500000, &active);
    CHECK(active && m.open_pm == 300);

    /* audio arriving late: the clock waits for it and does not run ahead */
    vt_player_begin_reply(p, 24000);
    CHECK(vt_player_add_segment(p, a));
    vt_player_on_pcm(p, 12000, t0);                       /* 250 ms of audio */
    m = vt_player_sample(p, t0 + 2000000, &active);       /* 2 s later, nothing more arrived */
    CHECK(p->pos_us == 250000);
    vt_player_on_pcm(p, 12000, t0 + 2000000);             /* 250 ms more now */
    m = vt_player_sample(p, t0 + 2100000, &active);
    CHECK(p->pos_us == 350000);                           /* the stall was not played */

    /* a rate of 22050 changes the segment length: 44100 bytes = 1 s */
    vt_player_begin_reply(p, 22050);
    vt_segment_t *c = make_segment(3, 44100, "aa", 1000);
    CHECK(vt_player_add_segment(p, c));
    CHECK(p->len_us[0] == 1000000);
    vt_player_begin_reply(p, 5);                          /* implausible rate -> 24000 */
    CHECK(p->rate == 24000);

    /* the end of the reply lets the last shape play out and close */
    vt_player_begin_reply(p, 24000);
    vt_segment_t *d = make_segment(4, 24000, "aa", 500);
    CHECK(vt_player_add_segment(p, d));
    vt_player_on_pcm(p, 24000, t0);
    m = vt_player_sample(p, t0 + 5000000, &active);
    CHECK(p->pos_us == 500000 && m.open_pm == 420);       /* stuck at the end: no more PCM yet */
    vt_player_end_reply(p);
    m = vt_player_sample(p, t0 + 5100000, &active);
    CHECK(m.open_pm == 0);
    m = vt_player_sample(p, t0 + 5200000, &active);
    CHECK(!active && p->count == 0);

    /* interrupt: reset leaves the mouth closed and the queue empty */
    vt_player_begin_reply(p, 24000);
    CHECK(vt_player_add_segment(p, a));
    vt_player_reset(p);
    m = vt_player_sample(p, t0, &active);
    CHECK(!active && m.open_pm == 0 && p->count == 0);

    /* the queue is bounded: the ninth timeline is dropped and counted */
    vt_player_begin_reply(p, 24000);
    for (int i = 0; i < VT_MAX_SEGMENTS; i++) {
        CHECK(vt_player_add_segment(p, a));
    }
    CHECK(!vt_player_add_segment(p, a) && p->dropped == 1);

    /* the speaker's own position replaces the virtual one */
    vt_player_begin_reply(p, 24000);
    CHECK(vt_player_add_segment(p, a));
    vt_player_on_pcm(p, 48000, t0);
    vt_player_set_played_us(p, 700000);
    m = vt_player_sample(p, t0 + 9000000, &active);
    CHECK(active && p->pos_us == 700000 && m.open_pm == 420);

    free(a); free(b); free(c); free(d);
    free(p);
}

static void test_reasm(void)
{
    char buf[64];
    ws_reasm_t r;
    const char *out = NULL;
    size_t n = 0;
    ws_reasm_init(&r, buf, sizeof(buf));

    /* a whole frame in one piece: delivered in place, nothing copied */
    const char *whole = "{\"type\":\"ready\"}";
    CHECK(ws_reasm_feed(&r, 1, whole, 16, 16, 0, true, &out, &n) == WSR_TEXT && out == whole && n == 16);

    /* three pieces of one frame */
    const char *msg = "0123456789abcdefghij";   /* 20 bytes */
    CHECK(ws_reasm_feed(&r, 1, msg, 8, 20, 0, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 1, msg + 8, 8, 20, 8, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 1, msg + 16, 4, 20, 16, true, &out, &n) == WSR_TEXT);
    CHECK(n == 20 && memcmp(out, msg, 20) == 0);

    /* a message in two frames: text, then a continuation */
    CHECK(ws_reasm_feed(&r, 1, msg, 10, 10, 0, false, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 0, msg + 10, 10, 10, 0, true, &out, &n) == WSR_TEXT);
    CHECK(n == 20 && memcmp(out, msg, 20) == 0);

    /* too large: dropped whole, and the next message is fine */
    char big[100];
    memset(big, 'x', sizeof(big));
    CHECK(ws_reasm_feed(&r, 1, big, 50, 100, 0, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 1, big + 50, 50, 100, 50, true, &out, &n) == WSR_DROPPED);
    CHECK(ws_reasm_feed(&r, 1, whole, 16, 16, 0, true, &out, &n) == WSR_TEXT);

    /* a piece of a message that never started, a continuation with nothing before it, control frames */
    CHECK(ws_reasm_feed(&r, 1, msg, 4, 20, 8, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 0, msg, 4, 4, 0, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 9, msg, 4, 4, 0, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 8, msg, 2, 2, 0, true, &out, &n) == WSR_NONE);

    /* a new text frame abandons a half-collected one */
    CHECK(ws_reasm_feed(&r, 1, msg, 8, 20, 0, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(&r, 1, whole, 16, 16, 0, true, &out, &n) == WSR_TEXT && n == 16);

    /* binary: every piece is passed on, also the continuation frames of a fragmented one */
    CHECK(ws_reasm_feed(&r, 2, msg, 8, 20, 0, true, &out, &n) == WSR_BINARY && n == 8);
    CHECK(ws_reasm_feed(&r, 2, msg + 8, 12, 20, 8, true, &out, &n) == WSR_BINARY && n == 12);
    CHECK(ws_reasm_feed(&r, 2, msg, 10, 10, 0, false, &out, &n) == WSR_BINARY);
    CHECK(ws_reasm_feed(&r, 0, msg, 10, 10, 0, true, &out, &n) == WSR_BINARY && n == 10);
    CHECK(ws_reasm_feed(&r, 0, msg, 10, 10, 0, true, &out, &n) == WSR_NONE);   /* the binary one is over */

    /* reset forgets a message in progress */
    CHECK(ws_reasm_feed(&r, 1, msg, 8, 20, 0, true, &out, &n) == WSR_NONE);
    ws_reasm_reset(&r);
    CHECK(ws_reasm_feed(&r, 1, msg + 8, 12, 20, 8, true, &out, &n) == WSR_NONE);

    /* bad arguments */
    CHECK(ws_reasm_feed(&r, 1, NULL, 4, 4, 0, true, &out, &n) == WSR_NONE);
    CHECK(ws_reasm_feed(NULL, 1, msg, 4, 4, 0, true, &out, &n) == WSR_NONE);
}

int main(void)
{
    test_parse();
    test_shapes();
    test_player();
    test_reasm();
    printf("%d checks, %d failed\n", g_checks, g_failed);
    return g_failed ? 1 : 0;
}
