"""Checking a synthesised sentence against its text: the voice service's own speech-to-text reads the WAV back (`POST /transcribe` with word times), and the words
heard are compared with the words written.

What it finds: a voice engine that adds sounds after the last word (a full stop at the end of a piece can make XTTS say "punto tale" or "it"), a word dropped or
changed, a piece read as something else. `compare` gives a similarity, the extra words at the tail with the time the last real word ends (so the tail can be cut off),
and the number of words missing. Digits are left out of the comparison: how a number is said is the engine's and the speech-to-text's to agree on, and the
script is asked to write numbers in words. Standard library only."""
from __future__ import annotations

import difflib
import http.client
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
TAIL_PAD_S = 0.12                                  # a breath kept after the last real word when the tail is cut
SIMILAR_OK = 0.82                                  # below this a piece is read again
TIMEOUT_S = 180.0


@dataclass
class Check:
    similarity: float = 1.0
    heard: str = ""
    extra_tail: List[str] = field(default_factory=list)
    trim_at_s: float | None = None                 # where the last written word ends in the audio, when words came after it
    missing: int = 0
    ok: bool = True


def fold(word: str) -> str:
    """The word lower-cased and without accents, so that a hyphen, a case or a missing accent in the transcript is not a difference."""
    return "".join(c for c in unicodedata.normalize("NFD", word.casefold()) if unicodedata.category(c) != "Mn")


def tokens(text: str) -> List[str]:
    return [fold(m.group(0)) for m in TOKEN.finditer(text)]


def _groups(words: List[Dict[str, Any]], lang: str) -> List[Dict[str, Any]]:
    """The transcript's words with the pieces of one number or date joined, so that they can be spelled together: a transcript writes "november 12-én" as "november", "12" and
    "-én", and "4,5" as "4" and ",5"; spelled one by one they are not the words the voice said."""
    from tadamoo.speech import textnorm
    months = {m.casefold() for m in textnorm.MONTHS.get(lang, [])}
    out: List[Dict[str, Any]] = []
    run = False                                                    # the last group is a number's
    for w in words:
        text = str(w.get("text", "")).strip()
        number = bool(re.search(r"[\d%]", text))
        glued = bool(re.match(r"^[,.\-]\s*\w", text)) and run
        if run and (number or glued):
            prev = dict(out[-1])
            prev["text"] = prev["text"] + ("" if glued or re.match(r"^[,.\-]", text) else " ") + text
            prev["end_s"] = w.get("end_s", prev.get("end_s"))
            out[-1] = prev
            continue
        if number and out and re.sub(r"[^\w]", "", str(out[-1].get("text", ""))).casefold() in months:
            prev = dict(out[-1])
            prev["text"] = prev["text"] + " " + text
            prev["end_s"] = w.get("end_s", prev.get("end_s"))
            out[-1] = prev
        else:
            out.append({**w, "text": text})
        run = number
    return out


def compare(text: str, words: List[Dict[str, Any]], lang: str = "") -> Check:
    """`words` are the transcript's words, `{text, start_s, end_s}`, in order. With `lang` the transcript's digits are spelled in that language first (the speech-to-text
    writes "2026" where the voice said "kétezer-huszonhat"), each word of them keeping the time of the transcript word they came from."""
    want = tokens(text)
    if lang:
        from tadamoo.speech import textnorm
        heard = [(tok, w) for w in _groups(words, lang) for tok in tokens(textnorm.normalise(str(w.get("text", "")), lang))]
    else:
        heard = [(fold(m.group(0)), w) for w in words for m in TOKEN.finditer(str(w.get("text", "")))]
    got = [h for h, _ in heard]
    if not want:
        return Check(heard=" ".join(w.get("text", "").strip() for w in words))
    matcher = difflib.SequenceMatcher(None, want, got, autojunk=False)
    similarity = matcher.ratio() if (want or got) else 1.0
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    extra: List[str] = []
    trim = None
    if blocks:
        last = blocks[-1]
        end_index = last.b + last.size                        # the first transcript word after the last matched one
        extra = got[end_index:]
        if extra and last.a + last.size == len(want):          # everything written was said, then more
            trim = float(heard[end_index - 1][1].get("end_s") or 0) or None
        else:
            extra = []
    missing = len(want) - sum(b.size for b in blocks)
    check = Check(similarity=round(similarity, 3), heard=" ".join(str(w.get("text", "")).strip() for w in words).strip(), extra_tail=extra, trim_at_s=trim, missing=missing)
    check.ok = similarity >= SIMILAR_OK and not extra
    return check


WAIT_FOR_SERVICE_S = 0.0                           # a production sets it: how long the read-back waits for a speech-to-text that does not answer (a restart of the server) before it fails
RETRY_EVERY_S = 30.0


def transcribe(url: str, wav: bytes, lang: str, key: str = "") -> List[Dict[str, Any]]:
    """The words of a WAV with their times, from the voice service through the server. Where `WAIT_FOR_SERVICE_S` is set, a refusal or silence is retried every half minute until
    then, and said once on stderr; after that (or without it) the error is the caller's."""
    import sys
    import time
    deadline, said = time.monotonic() + WAIT_FOR_SERVICE_S, False
    while True:
        try:
            return _service_words(url, wav, lang, key)
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise
            if not said:
                said = True
                print(f"speech-to-text: {exc}; waiting for the voice service (up to {int(WAIT_FOR_SERVICE_S // 60)} min)", file=sys.stderr, flush=True)
            time.sleep(RETRY_EVERY_S)


def _service_words(url: str, wav: bytes, lang: str, key: str = "") -> List[Dict[str, Any]]:
    """One request to the voice service's speech-to-text."""
    parts = urlsplit(url)
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        headers["Authorization"] = "Bearer " + key
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=TIMEOUT_S)
    try:
        conn.request("POST", f"/transcribe?lang={lang}&segments=1&words=1", body=body, headers=headers)
        r = conn.getresponse()
        raw = r.read(8 << 20)
    except (OSError, http.client.HTTPException) as exc:
        raise OSError(f"the speech-to-text does not answer ({exc.__class__.__name__})") from None
    finally:
        conn.close()
    if r.status != 200:
        raise OSError(f"the speech-to-text refused (HTTP {r.status})")
    out: List[Dict[str, Any]] = []
    for seg in json.loads(raw).get("segments", []):
        out += [w for w in seg.get("words", []) if isinstance(w, dict)]
    return out
