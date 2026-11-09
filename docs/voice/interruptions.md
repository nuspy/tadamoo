# Speaking over the avatar (the PC's side)

When the person talks while the avatar is still speaking, the gateway does not simply stop. `voice/callbridge/interruption.py` keeps one
`InterruptionManager` per connection: it lowers the voice, decides what the speech means, and remembers the part of the answer that was not
spoken. The phone's side (what it detects and sends) is in `gateway.md`, *Interruptions (the phone's side)*.

## What happens

1. `user_speaking {state: "start"}` while an answer plays: the gateway sends `duck {level}` at once (`duck_level`, 0.3). The voice is lowered, never muted.
2. The speech arrives as a `start` with `barge_in: true`, PCM and `end`. The PC does not stop the answer for it: the audio goes to the STT while the
   answer keeps being sent. When the phone says the speech ended (`user_speaking {state: "end"}` or `end`) the STT is flushed and a thread waits for
   the words and decides. The answer's audio is not held back by the decision.
3. The decision is one of four actions, and the gateway answers the phone with its messages in this order:

| Action | What the PC does |
|---|---|
| `ignore` | `transcript ""`, `duck {level: 1}`, `done`. The answer went on |
| `finish_then_answer` | `duck {level: 1}`; the words are queued and answered after the answer ends (`transcript`, the reply, `done`) |
| `stop_and_integrate` | `reply_reset` (the bubble keeps what was spoken), `interrupt`, `duck {level: 1}`, then a new answer from the direct brain with a context note |
| `stop_and_answer_now` | the same cut; the new request is answered at once; the rest of the old answer is kept for the offer below |

Between turns (the answer was sent whole and the phone is still playing it) the same messages go out at once; inside a turn the cut is made by the
turn's own loop. A phone `start` or `text` that arrives meanwhile wins over the queued words.

## How the decision is made

**Rules first, no model** (`interruption_policy.classifica`): noise, a hallucinated STT phrase, the avatar's own echo and a nod of agreement
(`ok`, `sì`, `mmh`) are `ignore`; a stop word (`basta`, `aspetta`, `stop`, `senti` alone) is `stop_and_answer_now` and nothing is offered afterwards.
A nod or an echo never reaches the model.

**Grey zone: the technical session.** Everything else is judged by the technical session, the small separate slot of the local server, use
`interruption` (`tadamoo/side_uses.py`: `needs_session`, on by default, shown with its prompt in Settings > Inference > Manage uses). The service
imports nothing from `tadamoo`: `TechnicalJudge` posts a non-streamed `/v1/chat/completions` to the local server with the service token and
`cache_slot: "side_only"`, `side_use: "interruption"`, `reasoning_effort: "none"`, `response_format: json_object`, 60 tokens. `side_only` means the
server refuses the call (400) when the session is missing or too small instead of serving it from a conversation's slot; and
`serve/api/cache_slot.py` refuses a call that names a use switched off in the settings. Both are a fallback for the PC, never an error for the person.

The model gets one JSON object in a fence: what the avatar already said, what is left, the person's previous request, the person's words, how long
they spoke and how loud. The prompt says they are data and never instructions; backticks are removed from the data so the fence cannot be closed
from inside. It must answer one JSON object:

    {"relation": "related" | "new", "urgency": "low" | "normal" | "high",
     "action": "ignore" | "finish_then_answer" | "stop_and_integrate" | "stop_and_answer_now"}

The first object of the answer is read; text around it and other keys are ignored; a field outside its set makes the whole answer unusable.
`stop_and_integrate` needs `relation: "related"`. Only `action` decides: `urgency` is recorded and never stops an answer by itself, whatever
words the person used. With the harness brain (the agent may already have acted) `stop_and_integrate` becomes `stop_and_answer_now`.

**Fallback.** The call has a hard deadline (`judge_timeout_ms`, 800 ms by default): a thread makes it and is abandoned when the time runs out.
A timeout, an error, no session, an unusable answer or an inconsistent one gives `finish_then_answer` (the avatar finishes, then answers: nothing the
person said is lost and nobody is cut off) with `degraded: true`. Two failures in a row, or a refusal by the server, leave the model alone for 30 s.

## What was spoken, and the integration

The manager records every sentence of the answer and every audio segment sent to the phone with its length. The phone plays at the speed of the
audio, so the time since the first segment says how much was spoken: whole segments, and a share of the one playing, counted in words. That gives
*spoken* and *left* (an estimate: the phone's buffer is not observed). For `stop_and_integrate` the brain (`ModeBrain.respond_stream(...,
context_note=...)`, direct brain only, this turn only, added to the system message) gets: *You were saying: <spoken>... Only that part was heard;
the rest was not said. The person interrupted with: <words>. Continue your answer taking it into account.*

With the duplex engine (`duplex_motore_nuovo`) the same manager decides in `_attendi_motore`: `stop_and_integrate` finishes the sentence being
spoken, cuts the ones after it, and the brain writes the rest with the note (`duplex_integra_da_s` is the least speech still to come for that;
otherwise the answer is stopped and the new request answered). The engine does not make the offer below.

## The offer to go on

After `stop_and_answer_now`, once the new answer has been sent, its audio has played and the person has been quiet for 2 s, the avatar says
*Do you want me to continue with what I was saying?* / *Vuoi che continui con quello che stavo dicendo?* (the conversation's language) and sends
`resume_offer {text, question, lang}`, where `text` is what was left. Accept by voice (a short *yes*, *sì*, *continua*, *go on*, in a turn or over
the question) or by `resume {accept: true}`: the PC says `text` with the speech engine, without asking the brain, and sends `turn_done`. *No*,
*lascia stare* or `resume {accept: false}` drops it. Anything else is a new turn and the offer waits for the next quiet. An offer nobody answers
is taken back after 60 s with `resume_offer {text: ""}`. A stop word leaves nothing to offer.

## Settings and the log

`voice.json` `interruptions`, set in Settings > Voice > *Speaking over the avatar* (`GET`/`POST /api/voice/interruptions`, this PC only):

| Field | Default | Range |
|---|---|---|
| `enabled` | true | off: a `start` while the reply plays stops it, as without the manager |
| `judge_timeout_ms` | 800 | 200 to 5000 |
| `duck_level` | 0.3 | 0 to 0.9 |
| `resume_offer` | true | |

A change applies from the next phone connection. Switching the use `interruption` off in the technical session's settings has the same effect as
no session: the rules decide and the grey zone is `finish_then_answer`.

Every decision is one log line, `gateway: interruption '<48 chars>' decision=<rules|model|fallback> action=<action> reason=<why>`, and a row in
`GET /interruptions` on the service (loopback, bearer token; the last 20 across connections: `heard` at most 80 characters, `action`, `reason`,
`source`, `relation`, `urgency`, `degraded`, `ms`, `level`).

## What is proven and what is not

Proven with stand-ins (`voice/tests/test_interruption_manager.py`, `test_interruption_gateway.py`, `serve/tests/test_voice_interruptions.py`): every
action for every relation and urgency, the fallbacks, injection, the order of the messages, the offer, the settings and the page. Not proven: a model's
real answers and its 800 ms on a loaded PC, how good the spoken/left estimate is against a real phone's buffer, and a real conversation.
