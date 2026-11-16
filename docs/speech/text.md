# Speech

## The text made fit for the voice (`textnorm.py`)

One ordered rule table, `RULES`, one place to improve: `(id, what it does and why, function)`. `normalise(text, lang, pronunciations)` runs it; language tables (abbreviations, words
for symbols, month names, letter names, acronyms read as words) are dictionaries at the top, one entry for each language (hu, it, en), and `pronunciations` from the script (written
form to the way to write it so that the engine says it right) goes first.

| Rule | Does |
|---|---|
| markup | markdown, links, HTML: the text stays, the marks go |
| addresses | e-mail addresses go; a link or a domain becomes the site's name (telex.hu is said Telex), never "http" |
| emoji | emoji and emoticons go |
| dates | 2026-11-12 and 12.11.2026 become day, month's name and year in the language's order |
| numbers | thousands separators go; Hungarian numbers, decimals, percentages, clock times and dates ("2026. november 12-én") are spelled in Hungarian; so are ordinals ("4. és 5. klaszter", "312. cikk") and numbers with an ending ("2027-ben", "1375-ös", a period "2028-2034-es" as "A és B közötti") |
| symbols | % & / @ + = and currencies become words; a currency in front of a number goes behind it |
| quotes | quotation marks of every kind go (typographic, guillemets, Hungarian), apostrophes inside words stay |
| brackets | an aside is read between commas |
| dashes | ellipses, dashes, line breaks are a pause |
| abbreviations | dr., pl., kb., stb., sig., ecc., Mr. ... spelled out |
| initials | the dot of an initial goes |
| acronyms | a word if the language's list says so (NATO), else letter by letter with the language's letter names (EU is said "é ú") |
| punctuation | repeated marks, colons, semicolons, spaces before marks |
| repeats | a name said twice because a link followed it is said once |
| stray | any other symbol goes |

A piece sent to the engine never ends in a full stop (`strip_end`). Measured on Italian with XTTS v2, 15 readings each: with the stop 7 pieces had spurious words at the end ("punto",
"punto tale", a babble), without it none. That is the "punto tale" of the first production. For Hungarian a piece ends in a comma (`speech.END_MARK`): without one XTTS often cuts the last
syllable ("elnöke" read "elnök"); the mean error of the same sentence over three readings was 0.17 with no mark, 0.19 with a stop, 0.10 with a comma, 0.21 with "...". The table of offending
patterns is `tests/test_newscast_speech.py` (one line for each, by language).

## The read-back (`voicecheck.py`, `speech.one_piece`)

Each piece is read back by the voice service's speech-to-text (`/transcribe`, language, word times) and compared with its text (accents and case ignored, the transcript's digits spelled
out first). Sounds after the last written word are cut off at the end of the last real word; a piece whose similarity is under 0.82 is read again, up to `TRIES` times (the engine
samples, so another reading is another one) and the best reading is kept. The report of each piece (what was heard, how close, how many tries, what was cut) goes to the manifest and to the
review page; a piece that never matches is flagged, not hidden. A speech-to-text that does not answer leaves the voice unchecked and says so.

## Hungarian (what was found, 12 November 2026)

Only XTTS v2 of the engines in the voice service speaks Hungarian. Chatterbox, CosyVoice 3 and Orpheus refuse it, Kokoro reads it as Italian or English (word error 1.2), Qwen3-TTS and Dia
are not installed, F5-TTS and Fish (LTX Studio) have no Hungarian model. Candidates outside the service were tried on the same three sentences: Piper hu_HU (anna, berta, imre, CC0
dataset) and MMS-TTS (CC-BY-NC), and compared with eight XTTS built-in voices by the read-back (Whisper medium): word error between 0.06 and 0.26 for the XTTS voices and Piper anna,
0.22 for MMS; the sample is three sentences, and two readings of the same voice differ by 0.1, so the order is not a ranking. What fails: the engine is not stable on "Brüsszelben" (read
"Brüsszában", "Brüsselben", "bűszerben": right in about one reading of four), on the Hungarian "cs" at the start of a sentence and on the last syllable; foreign names ("Kovács" read
"Kuvács"). Respelling with a space ("Brüssz elben") made the pieces run together; splitting a very long word ("elszámoltatható ságot") helped. Reading each piece several times and keeping the
one the speech-to-text likes best is what the pipeline does. The things to change if it is not enough: a pronunciation list per name (tested by the read-back), shorter sentences, an engine
fine-tuned on Hungarian, a free cloud voice (Edge hu-HU-NoemiNeural: not used, it needs the person's yes).
