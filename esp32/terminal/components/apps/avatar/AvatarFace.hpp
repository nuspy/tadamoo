#pragma once

#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "lvgl.h"
#include "viseme_timeline.h"

/**
 * The avatar of the `visemes` mode: a drawn face whose mouth follows the
 * timeline the PC sends (docs/voice/gateway.md, Render modes).
 *
 * It does not draw the person's photo: the sprites of the avatar folder are not
 * downloaded, so the face is the same for everyone and the mouth shape is the
 * part that carries the speech. The shapes are those of docs/voice/avatar.md
 * (opening and width per viseme), cross-faded between items.
 *
 * Two sides. The feed (replyStart, pcm, timeline, replyEnd, stop, played) is
 * called from the WebSocket task and touches only the clock, under a mutex.
 * Drawing runs in an LVGL timer, which already holds the LVGL lock.
 */
class AvatarFace
{
public:
    AvatarFace() = default;
    ~AvatarFace();
    AvatarFace(const AvatarFace &) = delete;
    AvatarFace &operator=(const AvatarFace &) = delete;

    /**
     * Creates the face inside `stage` (width x height in pixels), allocates the
     * clock and runs the self-check of the timeline code. False leaves nothing
     * running: the caller must not declare `visemes`.
     */
    bool begin(lv_obj_t *stage, int width, int height);

    /** Stops the timer and forgets the objects (LVGL deletes them with the screen). */
    void end();

    bool isReady() const { return _ready; }

    /** True while the PC serves `visemes`: shows the face and accepts the feed. */
    void setActive(bool active);
    bool isActive() const { return _active; }

    // ---- the feed, any task
    void replyStart(int sample_rate);
    void pcm(size_t bytes);
    void timeline(const vt_segment_t *segment);
    void replyEnd();
    /** An interrupt, a new turn or a lost connection: the mouth closes. */
    void stop();
    /** The speaker's own playback position, when there is a speaker. */
    void played(uint64_t played_us);

private:
    static void tickCb(lv_timer_t *timer);
    void tick();
    void apply(vt_mouth_t mouth);

    volatile bool _ready = false;
    volatile bool _active = false;

    SemaphoreHandle_t _mutex = nullptr;
    vt_player_t *_player = nullptr;

    lv_timer_t *_timer = nullptr;
    lv_obj_t *_head = nullptr;
    lv_obj_t *_mouth = nullptr;

    int _mouth_w0 = 0;       // neutral mouth width in pixels
    int _mouth_dy = 0;       // mouth centre below the head centre
    int _last_w = -1;
    int _last_h = -1;
};
