# Telephony

The voice service places and answers phone calls on two transports, with one set of safety rules that every call passes through. The rules are
in `voice/callbridge/call_policy.py` (the decisions and the audit) and `voice/callbridge/call_service.py` (placing, answering, ending).

## Transports

| Transport | Link | Code |
|---|---|---|
| `gsm` | USB modem (SIM800C) on a serial port; the audio goes by file, one turn at a time | `gsm_modem.py`, `gsm_call.py`, `gsm_audio.py` |
| `android` | a phone over Bluetooth hands-free; the dialer app on the phone is told what to do over the network | `orchestrator.py`, `phone_client.py`, `phone_discovery.py`, `audio_hfp.py`, `ks_native.py` |

`transport` in the settings is `off` (the default), `auto` (the modem when it is registered on the network, else the phone), `gsm` or `android`
(`transport.py`). The choice is made again at every call. While telephony is off no serial port, phone or network link is opened and every call
route answers 409 `telephony_off`. When incoming auto-answer is on, the modem is opened as soon as the settings say so, to hear it ring.

## Settings (`telephony` in voice.json)

| Key | Values | Default | Meaning |
|---|---|---|---|
| `transport` | `off`, `auto`, `gsm`, `android` | `off` | which line is used |
| `outgoing_approval` | `manual`, `auto` | `manual` | calls started by the agent or by a program: asked, or approved by the service |
| `ai_disclosure_on_start` | true, false | true | the first sentence of every call says the caller is an AI |
| `incoming_auto_answer` | true, false | false | the AI answers a call that rings (GSM and Android) |
| `phone_calls` | true, false | false | the phone app with its key may start a call by hand through this PC (a setting of the server, the service does not read it) |
| `on_behalf_of` | text, 80 characters | empty | the name the AI gives when it calls |
| `premium_prefixes` | list of digit prefixes, up to 200 | the built-in list (IT, US/CA, UK, FR, DE, satellite and international premium) | numbers the agent and programs never call; `[]` turns the block off |
| `auto_contacts_only` | true, false | false | with `auto`, only the numbers in `contacts` are approved without asking; any other number asks as in `manual` |
| `contacts` | list of numbers (6 to 15 digits), up to 500 | empty | the numbers `auto_contacts_only` trusts |

Settings > Voice has a card for them; the server writes the file atomically and refuses a value outside the list (`serve/voice_settings.py`).
A damaged value reads as its default. The service reads the file again when it changes.

## Who started a call

| Origin | How | Approval |
|---|---|---|
| `user` | `POST /call` (by hand); the phone app's call arrives here too, through `POST /api/voice/phone/call`, which the server accepts only on the listener for phones with a key and with `phone_calls` on | none |
| `agent` | `POST /external/call` with `X-Call-Origin: agent` (the phone tools of the agent, which ask the person in the chat) | `manual`: recorded as asked by the agent; `auto`: approved |
| `program` | `POST /external/call` without the header | `manual`: waits for the person; `auto`: approved |

In `manual` a program gets `202 {"code": "approval_required", "approval_id"}` and nothing is placed. The person answers in Settings > Voice
(or `POST /call/approvals/<id>/approve|deny`); the program then sends the same request again with `approval_id`. An approval is for one number
and one greeting, is used once and expires after five minutes; at most ten wait. `auto` approves without asking and the call is still
logged and still opens with the disclosure. The service trusts the header because only a holder of the service token can send it: the
route on TaDaMoo's port (`serve/voice_compat.py`) must not copy it from an outside caller.

## Rules every call follows

- **Numbers.** Cleaned of spaces, dashes and brackets. A call by hand takes digits, `+`, `*`, `#` (2 to 20); the agent and programs take `+`
  and 6 to 15 digits. Emergency numbers (112, 113, 114, 115, 117, 118, 911, 999, 000, 110, 119, 120, 15, 17, 18, 100 to 102, 108, 190 to 196,
  and the other country numbers listed in `EMERGENCY`, alone, after a country code with or without `+` (`+33 17`, `39112`) or after a switchboard
  prefix (`0112`, `9911`)) are refused with 403 for every origin.
- **Premium-rate numbers.** The agent and programs (not a call by hand) are refused with 403 `premium_number` when the number starts with a prefix
  of `premium_prefixes`, in `manual` as well as `auto`. A prefix with a leading `+` is read against a number written with a country code
  (`+` or `00`; the national zero of UK, FR and DE after the code is ignored), a prefix without it against a number written nationally. The
  built-in list holds `89x`, `144`, `166` (IT), `900`, `976` (US/CA), `09x`, `070`, `087x` (UK), `089x`, `0836` (FR), `0900`, `0137`, `0190`
  (DE), the `118` directory services and the satellite and international premium codes (`+870` to `+874`, `+881` to `+883`, `+979`). It is
  a starting point, not a complete list of the world's tariffs; edit it in Settings > Voice.
- **Who dials what is the person's risk.** `auto` lets any program place calls without asking; the risks are cost (numbers outside the list),
  nuisance and harassment of third parties, and calls made under the person's name. `auto_contacts_only` is off by default; turning it on
  limits `auto` to the person's own numbers. A call by hand is never limited by either list.
- **Disclosure.** With the setting on, a call by hand gets the sentence "I am a virtual assistant based on artificial intelligence..." put in
  front of its greeting. A greeting from the agent or a program that lacks a marker (`intelligenza artificiale`, `assistente virtuale`,
  `artificial intelligence`, `virtual assistant`) is refused with 400 `no_disclosure`; a missing one with 400 `greeting_missing`. With the
  setting off the greeting is used as it is and the audit row says `disclosure: off`. A call answered by the AI opens with the same kind of sentence.
- **One call at a time**, 409 `busy` otherwise. A slot whose call never ended is freed after the longest call plus two minutes.
- **No retry.** A placement is one attempt. A failure frees the slot and returns 502 or 500; only the caller can decide to try again.
- **Guards kept.** `gsm_max_call_s` (600 s) and `gsm_max_empty_turns` (3) end a GSM call; the suspend switch (`sospensione.py`) stops answering.
- **The brain of a call** is a fresh direct brain: no tools, no files, no agent. What the other side says is untrusted text.
- **Incoming.** Not answered unless `incoming_auto_answer` is on; each ignored call is written to the audit with the reason
  `auto_answer_off`. The Android phone is watched for calls only while auto-answer is on.
- **Audit.** No call is placed or answered when its first row cannot be written.

## Audit

`call-audit.jsonl` next to voice.json, one JSON line per event, renamed to `.1` at 2 MiB:

```json
{"ts":"2026-11-09T10:00:00+00:00","event":"start","id":"a1b2c3d4e5f6","direction":"outgoing","transport":"","number":"***567","origin":"agent","approval":"manual","disclosure":"on"}
{"ts":"2026-11-09T10:01:12+00:00","event":"end","id":"a1b2c3d4e5f6","direction":"outgoing","transport":"gsm","number":"***567","origin":"agent","approval":"manual","disclosure":"on","duration_s":72.0,"outcome":"completed"}
{"ts":"...","event":"refused","direction":"outgoing","transport":"","number":"***112","origin":"program","reason":"emergency_number"}
```

`outcome` is `completed`, `no_conversation`, `hung_up`, `failed`, `error` or `abandoned`. `approval` is `not_needed`, `manual`, `auto`,
`auto_answer` or `explicit`; `origin` also takes `caller` for an incoming call. The transcript is never written here.

## Routes of the service

All need the service token. Besides the call routes of `service.md`:

| Route | Does |
|---|---|
| `POST /call` | `{number, greeting?, voice?, lang?, transport?}`, a call by hand; 200 `{status: "calling", transport}` |
| `POST /hangup` | ends the call, `{"status": "ok"}` |
| `GET /transport` | the transport chosen and what was detected; `?refresh=1` detects again |
| `POST /external/call` | `{number, greeting, voice?, lang?, transport?, approval_id?}`; 200 `{call_id, status, transport}` |
| `POST /external/<id>/say` | `{text}`, the next line to say |
| `GET /external/<id>/heard` | `?after=N&timeout_s=T` (at most 60), waits for the next line of the other side |
| `GET /external/<id>/transcript`, `/state` | the lines so far; status, transport and duration |
| `POST /external/<id>/hangup` | ends the call, returns its final state |
| `GET /call/approvals` | `{pending: [{id, number, greeting, age_s}], mode}` |
| `POST /call/approvals/<id>/approve`, `/deny` | the person's answer |
| `GET /call/audit` | `?limit=` the last rows, numbers masked |

`GET /status` carries `telephony`: transport, approval mode, disclosure, auto-answer and whether a call runs; it reads settings only.
The voice tab reaches the approval and audit routes through the fixed list of `serve/voice_proxy.py`.

## What the agent does with a call

The phone tools (`tadamoo/integrations/telephony_tools.py`) ask the person before every `phone_call`, send `X-Call-Origin: agent`, and never
place a call again after a doubtful answer. What the other side says (`phone_listen`) and the transcript that `phone_hangup` returns are
fenced as untrusted data and taint the session.

## The dialer app

The phone side of the `android` transport is `android/dialer/` (Kotlin, built with Gradle; no build output and no wrapper in the repository, see its
README). The phone serves a small control API on port 8080 of the phone, protected by a pairing token that the person types into the app; without
one the app's server does not start. The PC side reads the same token from the environment variable `TADAMOO_PHONE_TOKEN` of the service (the
desktop app is meant to set it from the credential store; that wiring is not done). It is never written in a file of the repository.

## Tests

`voice/tests/test_call_policy.py`, `test_external_policy.py`, `test_telephony_audit.py` (a router that places nothing, a fake clock, no serial port,
no network), `serve/tests/test_voice_telephony_settings.py`, `tests/test_phone_service_rules.py`. A real modem or phone is not exercised.
