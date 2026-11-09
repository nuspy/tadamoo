# Meetings, video calls and the copilot

The voice service can bring an AI assistant into a Google Meet, Microsoft Teams or Zoom meeting as a guest, and can show the person private
suggestions during a call. Webex, Slack and Telegram are not built. The code is `voice/callbridge/meetbot/` (the bot), `copilot.py`,
`api_meetings.py` (routes) and `voice/meetings_service.py` (the pieces a session needs, built from the service's own STT, TTS, brains and avatar).

## The consent rule

This is the safety rule of the feature and is not a setting that can be turned off (`meetbot/consent.py`, `meetbot/bot.py`).

1. **The person confirms.** A join needs `rights_confirmed: true` (the box "I have the right to bring an AI assistant to this meeting" on the page).
   It is written to the audit as `rights`. A join also needs the name of the person it acts for (`meetings.on_behalf_of`), because the announcement names them.
2. **The announcement comes first.** On entering, the bot posts in the meeting chat, in English and Italian, who it is (an AI assistant), on whose
   behalf, that it transcribes to write minutes and records no audio, and how to object (say or type "stop"). If speaking is possible it says the same by
   voice. If neither channel carries it, the bot leaves and has listened to nothing.
3. **Nothing is listened to or kept before that.** The audio capture and the speech recognition start after the announcement. What is heard is held in
   memory; no folder, no file and no meeting address is written until consent is given.
4. **The consent gate** then waits `consent_window_s` (20 to 300, default 45) for speech and chat:
   - an objection from anyone (stop, no, non acconsento, esci, leave, remove, don't record, and the other phrases of `consent.py`, matched as whole
     words, Italian and English), the host removing the bot, a call that drops during the window, or the person pressing Leave ends the session at once, deletes
     everything held, and leaves (with a goodbye in the chat, and by voice when speaking is allowed, for an objection or a missing consent);
   - `consent_mode: window` (default) stays when the window ends with no objection; `explicit` needs a yes (ok, yes, va bene, consent, ...) from anyone and leaves
     after twice the window without one. A sentence with an objection and a yes counts as an objection;
   - after consent a plain stop word still ends the session and deletes the transcript; a bare "no" does not (it is only an objection while consent is asked for);
   - a participant who joins later gets a short announcement in the chat (at most one every 30 seconds).
5. **The bot's own words are not objections.** Its announcement in the chat, and its voice if it comes back through the capture, are recognised as its own
   (`consent.is_echo`); a lone "stop" is never taken for an echo.

## What is stored

| What | Where | Holds |
|---|---|---|
| Audit | `meetings-audit.jsonl` next to `voice.json` (rotated at 2 MiB) | `join` (platform, the right confirmed, mode and window, speak on or blocked, names masked as `M*** R***`), `announced` (chat, voice), `consent`, `reannounced`, `spoken` (kind only), `end` (reason, outcome, objections with their word only, participants, duration, whether kept). No transcript, no address |
| Transcript | `<user data>/voice/meetings/<id>/transcript.jsonl` | text lines with the speaker, only after consent, until the person deletes the meeting |
| Minutes | same folder, `analysis.json`, `summary.md`, `meta.json` | summary, highlights, agreements, tasks per person; the address without its query (a passcode lives there) |

Audio is never kept. If consent is withdrawn after it was given, the folder is emptied and only a `meta.json` stub remains (`deleted: true`, the reason), so the
person sees what happened. `DELETE /meetings/<id>` removes the folder, the transcript and the minutes with it. A join whose first audit row cannot be written does not happen.

## Joining and listening

The browser is a Chromium driven by Playwright, visible on the desktop. The bot always joins with its microphone and camera **off**, because the device a meeting picks may be the person's own; the browser is
given permission to use them only when speaking or the camera is allowed. Playwright and its
Chromium are not installed by TaDaMoo: when missing, the join ends with `browser_missing` and the reason. Listening uses the service's own STT, which must be loaded
(`409 not_loaded` otherwise) and reads its model from the models folder only. The PC's output is captured as loopback (WASAPI on Windows; the monitor of the
default PulseAudio sink on Linux), so everything the PC plays is heard, not only the meeting. Speakers come from the platform's page, best effort ("Unknown" when it
does not say). When the meeting ends with consent given, the minutes are written by the analysis pipeline through the direct brain; `analysis_state` is
`pending`, `analyzing`, `done` or `error`. The minutes are not pushed to a wiki: TaDaMoo's wiki integration is read only.

## Speaking

Off by default (`meetings.speak`). The bot speaks only when all of this holds: the setting is on; the announcement is out and consent is given (the announcement and
the goodbye are the exceptions); a virtual microphone exists; the TTS is loaded; a person addressed it by name or wake phrase (or `proactive` is on, then at most once
every 45 seconds); and nobody else is speaking (it waits 1.2 s of silence, gives up after 20 s, and stops if someone starts). The reply is the **direct brain's**
`ask_stream`, which has no tools: the meeting's words sit inside tags in the prompt as untrusted data, and a harness brain is refused even when it is the current mode. The
bot never acts on instructions found in the meeting.

- **Virtual microphone:** a virtual audio cable that the person installed (VB-CABLE, VoiceMeeter or similar), found by its output device name or by `meetings.virtual_mic`. TaDaMoo installs
  nothing. When there is none the page says "no virtual microphone found: install a virtual audio cable and pick it in Settings > Voice" and the bot stays silent.
  The meeting must also use the cable's other end as its microphone (the Windows default recording device, or the choice in the meeting's settings); the adapters do not select it. The bot's
  microphone is muted except while a sentence plays (it is switched on just before and off just after); if it cannot be switched on, nothing is spoken.
- **Avatar:** when the avatar function is loaded, its lips are moved with the sentence (`speak_wav(mute=true)`). To show it as the camera, turn on the avatar's virtual camera (`avatar.md`), make it the camera
  the meetings use, and turn on `meetings.camera`: the bot then switches its camera on after consent. It is off by default because a meeting's default camera may be a real webcam.

## The copilot

Shift+F4 (with the hotkeys on in the Voice tab) or the Voice tab opens a private window with N suggested replies (1 to 6) for what the **other party** says. It listens to the loopback
only, never to the person's microphone; the text goes to the direct brain fenced as data; nothing is written to disk. The window is excluded from screen sharing and recording with
`SetWindowDisplayAffinity` (Windows, through `Desktop.exclude_from_capture`); where that is not possible, or when it fails, the window is **not opened** (a photograph of the screen is not
stopped). "Reply with audio" speaks a clicked reply through the virtual microphone under the rules above. The window is a Tk window in its own thread
(`desktop/suggestions.py`); the desktop layer's `Overlay` is a small status card and does not fit a list of suggestions.

## Calendar

Off and inert. Nothing is shipped to read a calendar: the person makes their own OAuth app (a desktop client in a Google Cloud project, or a public-client app registration in Azure
with `Calendars.Read`) and puts its values in the credential store (`tadamoo/llm/credentials.py`) as JSON in the entries `meetings/google` (`client_id`, `client_secret`) and
`meetings/microsoft` (`client_id`, `tenant`). The tokens the sign-in produces go to the same entries, never to a file. There is no form for the values yet ("configured manually").
Without them `configured()` is false and no library or network is touched. The events with a link an adapter takes appear in the planner (below) as items of source `google` or
`microsoft`; the calendar owns their title, address and time.

## Planner

The Meetings card of the Voice tab lists the meetings to attend (`meetbot/plan_store.py`, `planner.py`, `calendar_watch.py`; routes in `api.md`). The plan is `meetings_plan.json` next to the past
meetings, replaced whole through a temporary file. An item holds `title`, `platform`, `url`, `start` (stored in UTC; a request names its zone), `duration_min`, `source` (`manual`, `google`,
`microsoft`), `participate`, `role`, `persona`, `camera`, `speak`, `consent_mode` (empty: the setting) and `notes`; the planner adds `session_id`, `joined_at`, `last_error` and `last_note`.

- **Add, edit, delete.** By hand: the platform is found from the address by the adapter registry; an address no adapter takes is refused and the answer names the registered platforms.
  A calendar item is stored once the person changes something about it; its title, address and time are the calendar's (`calendar_owned`), it cannot be deleted here, and it joins only while the
  calendar still lists it. What started more than 14 days ago is forgotten; the list shows coming meetings first by time, then the last two days, latest first.
- **Role.** `listen` keeps the transcript and writes no minutes; `minutes` (default) also writes them; `speak` lets the bot answer under the rules of Speaking (`speak` follows the role).
- **Personality.** `persona` is the slug of a TaDaMoo personality. For that meeting the bot takes its text (put before a spoken reply as style, never as rules; its notes about the person are not
  taken), its voice (else the one the avatar wizard made) and its language. If the personality is gone the bot joins without it. The camera shows its avatar only when the item asks for it, the
  personality has an avatar, the avatar's virtual camera is on and the avatar function is loaded (the personality's avatar is started if another is active); otherwise the camera stays off, the join
  answer says `camera_blocked` (`no_persona`, `persona_missing`, `no_avatar`, `virtual_camera_off`, `avatar_not_loaded`) and the item shows it. The camera still comes on only after consent.
- **States.** `upcoming`, `running`, `done` (with `session_id`, the id of the record at `/meetings/<id>` when consent was given), `missed` (Participate was ticked, the start passed by more than 15
  minutes and the bot did not join) and `past` (not ticked).
- **The loop.** One thread (`CalendarWatcher`) turns every 15 s and does nothing while the master switch `meetings.autojoin` (default off) is off. When on, it reads the calendars at most every
  `calendar_poll_s` and joins an item with Participate ticked `lead_s` (60) seconds before the start, up to 15 minutes after, once, through the same path as a join from the page: the announcement
  and the consent rule apply in full and the audit says `rights: planned` and the personality. A refused join (another meeting in progress, STT not loaded, nobody named) is tried again every 30 s
  inside that window and the item shows why. Nothing joins by itself unless the person turned the master switch on and ticked that meeting. Join now (`rights_confirmed: true`) is a join from the page
  for that item and does not need the switch.

## Programs and the join route

`POST /meetbot/join` and the routes that can start the bot later (`POST /meetings/plan`, `PUT|PATCH|DELETE /meetings/plan/<id>`, `POST /meetings/plan/<id>/join-now`, `POST /calendar/events/<id>/autojoin`)
are refused to programs on TaDaMoo's port (`serve/voice_compat.py`) unless `meetings.allow_programs_join` is on (default off; Settings > Voice > Meetings). The refusal is `403` with
`code: programs_join_off` and the text in `error` (English) and `message_it`. The switch is read at every request. Reading the plan, leaving, and the other meeting routes are not gated. The
Voice tab does not go through that port (`serve/voice_proxy.py`), so it is not affected, and the consent rule applies to a program's join as to any other.

## Adding a platform

Write a subclass of `MeetingAdapter` (`meetbot/adapters/base.py`: `platform`, a static `matches(url)`, `join`, `in_call`, `leave`; the selectors and texts have safe defaults) and call
`register(MyAdapter)` in `meetbot/adapters/__init__.py`. `register` refuses a missing or taken name and a missing `matches`. `detect(url)` gives the platform name and `platform_names()` the list; the
planner, the routes and the page name only registered adapters. Add a fake-DOM test like `test_meet_adapters.py`.

## Settings (`meetings` in voice.json)

`speak` (false), `camera` (false), `proactive` (false), `consent_mode` (`window`), `consent_window_s` (45), `on_behalf_of`, `display_name`, `virtual_mic`, `wake_phrase`, `autojoin` (false, the master
switch of the planner), `allow_programs_join` (false),
`copilot_replies` (3), `copilot_audio_reply` (false), `copilot_hide_from_capture` (true). Settings > Voice > Meetings edits them; a bad value reads as its default.

## Routes

On the service: `POST /meetbot/join|leave`, `GET /meetbot/status`, `GET /meetings/list`, `GET|DELETE /meetings/<id>`, `GET /meetings/<id>/transcript`, `GET /calendar/status|events`,
`POST /calendar/<google|microsoft>/connect|disconnect`, `POST /calendar/events/<id>/autojoin` (sets Participate), `GET|POST /meetings/plan`, `PUT|PATCH|DELETE /meetings/plan/<id>`,
`POST /meetings/plan/<id>/join-now`, `POST /copilot/toggle`, `GET /copilot/state`. The Voice tab reaches them through the
fixed list of `serve/voice_proxy.py`; programs on this PC reach them on TaDaMoo's port (`api.md`, kind `local`). A program must send `rights_confirmed: true` too: it asserts the
right for the person.

## Tests, and what is not proven

`voice/tests/test_meet_consent_*.py` (every path of the rule, with a fake clock, adapter, STT, audio and brain), `test_meet_speak_rules.py`, `test_meet_adapters.py` (a fake DOM),
`test_copilot_suggestions.py`, `test_meet_settings.py`, `test_meetings_service.py`, `test_meeting_plan_store.py`, `test_meeting_plan_planner.py`, `test_meeting_plan_service.py`,
`serve/tests/test_voice_meetings_page.py`, `test_voice_meeting_plan_page.py` and `test_voice_compat_programs_join.py`. The page was checked in a headless browser against a faked API at 360x640 and 1280x900.
**Not proven, because it needs a real meeting, audio device or browser:** the selectors of the three platforms (written from their public pages), joining, the lobby, chat posting and reading, detection of a
removal, loopback capture and the STT hearing a real meeting, the virtual microphone and the camera in a real meeting, the Tk window and the capture exclusion, the calendar sign-ins, the loop joining
at the real time, the personality's avatar starting and being the meeting's camera, and everything on Linux.
