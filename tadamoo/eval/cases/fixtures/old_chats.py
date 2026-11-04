"""E13's material: twelve past conversations on different subjects and sixteen questions that ask for one of them in other words.

Each question shares no useful word with the text of its conversation: it uses an abbreviation (EU, RGPD, IRPEF, RTX), the other language, or a paraphrase.
Each conversation also carries the card a good technical session would write (title, summary, topics, the other names), written here by hand so that the
test needs no model. The right conversation of each question is `answer`.
"""
from __future__ import annotations

from typing import Any, Dict, List

from tadamoo.cards import Card


def conv(id: str, title: str, kind: str, turns: List[str], card: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": id, "title": title, "kind": kind, "turns": turns, "card": Card(title=title, source="model", **card)}


CONVERSATIONS: List[Dict[str, Any]] = [
    conv("eu_events", "Eventi dell'Unione Europea a novembre", "chat",
         ["Quali eventi dell'Unione Europea ci sono a novembre?", "Il vertice sul clima di Bruxelles il 14 novembre, poi il consiglio dei ministri a fine mese.",
          "E per il Parlamento?", "La plenaria di Strasburgo è la settimana dopo."],
         {"summary": ["Chiesti gli eventi europei di novembre.", "Vertice sul clima a Bruxelles il 14, consiglio dei ministri, plenaria a Strasburgo."],
          "topics": ["eventi europei", "calendario", "vertice sul clima"],
          "also_called": ["UE", "EU", "European Union", "summit", "Brussels", "EU summit", "events calendar", "meetings", "Parlamento europeo", "European Parliament"],
          "entities": ["Unione Europea", "Bruxelles", "Strasburgo"], "decisions": []}),
    conv("kernel_build", "Building sm_120 kernels", "agent",
         ["How do I build sm_120 kernels with nvcc?", "Use -arch=sm_120 and CUDA 12.8; the fatbin then runs on the new cards.", "Does it need extra flags?",
          "Add -lineinfo for profiling and keep -O3."],
         {"summary": ["Asked how to build kernels for sm_120.", "Use nvcc with -arch=sm_120 on CUDA 12.8, -O3, -lineinfo for profiling."],
          "topics": ["cuda build", "gpu architecture", "profiling"],
          "also_called": ["Blackwell", "RTX", "RTX 5090", "compile", "compile settings", "compiler options", "GPU compiler", "NVIDIA architecture", "CUDA toolkit", "sm120"],
          "entities": ["nvcc", "CUDA 12.8", "sm_120"], "decisions": ["use -arch=sm_120 with -O3"]}),
    conv("pasta_recipe", "Carbonara senza panna", "chat",
         ["Ricetta della carbonara senza panna", "Guanciale, pecorino, uova e pepe nero: niente panna, il cremoso viene dall'uovo e dall'acqua di cottura.",
          "Quanto guanciale per quattro?", "Circa duecento grammi."],
         {"summary": ["Chiesta la carbonara tradizionale.", "Guanciale, pecorino, uova, pepe; niente panna."],
          "topics": ["ricetta", "cucina romana", "pasta"],
          "also_called": ["egg-based Roman dish", "recipe", "Roman dish", "spaghetti", "cooking", "cured pork cheek", "eggs and cheese pasta", "cream-free"],
          "entities": ["carbonara", "guanciale", "pecorino"], "decisions": ["niente panna"]}),
    conv("tax_return", "Detrarre le spese mediche", "chat",
         ["Dichiarazione dei redditi: come detraggo le spese mediche?", "Con il modello 730 e le fatture: detrazione del 19 per cento oltre la franchigia.",
          "Servono anche gli scontrini della farmacia?", "Sì, se riportano il codice fiscale."],
         {"summary": ["Come detrarre le spese sanitarie.", "Si usa il 730 con fatture e scontrini parlanti, detrazione al 19 per cento sopra la franchigia."],
          "topics": ["detrazioni", "dichiarazione dei redditi", "spese sanitarie"],
          "also_called": ["IRPEF", "tax return", "income tax", "medical expenses", "deductions", "tax refund", "form 730", "healthcare costs", "modello 730"],
          "entities": ["730", "Agenzia delle Entrate"], "decisions": ["conservare fatture e scontrini parlanti"]}),
    conv("flutter_app", "Flutter app crashes on Android 14", "agent",
         ["Flutter app crashes on startup on Android 14", "Update the gradle plugin to 8.5 and raise the Kotlin version to 1.9.", "It still stops at the splash screen.",
          "Remove the old plugin registrant; the embedding v2 does not need it."],
         {"summary": ["The app died at start on Android 14.", "Gradle plugin and Kotlin were updated; the old plugin registrant was removed."],
          "topics": ["android crash", "gradle", "flutter embedding"],
          "also_called": ["mobile app", "app that would not launch", "launch failure", "Dart", "APK", "build.gradle", "mobile bug", "smartphone app"],
          "entities": ["Flutter", "Android 14", "Kotlin", "gradle"], "decisions": ["remove the old plugin registrant"]}),
    conv("guitar_lesson", "Accordi barrè sulla chitarra", "chat",
         ["Come imparo gli accordi barrè sulla chitarra?", "Esercitati con F maggiore, premi con l'indice e tieni il polso rilassato.", "Quanto devo esercitarmi?",
          "Dieci minuti al giorno bastano all'inizio."],
         {"summary": ["Come imparare i barrè.", "Partire da F maggiore, indice teso, dieci minuti al giorno."],
          "topics": ["chitarra", "accordi", "esercizio quotidiano"],
          "also_called": ["guitar", "guitar chords", "barre chords", "practise guitar", "music practice", "learning an instrument", "chords"],
          "entities": ["F maggiore"], "decisions": ["dieci minuti al giorno"]}),
    conv("mortgage", "Fixed or variable rate mortgage", "chat",
         ["Compare fixed vs variable rate mortgage options", "A fixed rate gives certainty; a variable one follows the Euribor and can fall or rise.", "Which is better for twenty years?",
          "If you can bear rises, variable; otherwise fixed."],
         {"summary": ["Fixed against variable rate for a home loan.", "Fixed is certain, variable follows the Euribor; depends on tolerance to rises."],
          "topics": ["home loan", "interest rate"],
          "also_called": ["mutuo", "tasso fisso", "tasso variabile", "mutuo a tasso fisso o variabile", "home loan", "mortgage rate", "prestito casa", "interest"],
          "entities": ["Euribor"], "decisions": ["variable only if rises can be borne"]}),
    conv("trip_japan", "Ten days in Kyoto and Tokyo", "chat",
         ["Plan a 10 day trip to Kyoto and Tokyo in April", "Book the Shinkansen early and see the cherry blossoms at Maruyama Park in Kyoto.", "Where to sleep in Tokyo?",
          "Shinjuku is practical for trains."],
         {"summary": ["A ten-day itinerary for Kyoto and Tokyo.", "Shinkansen booked early, cherry blossoms at Maruyama Park, stay in Shinjuku."],
          "topics": ["travel itinerary", "Kyoto", "Tokyo", "cherry blossom season"],
          "also_called": ["Giappone", "Japan", "viaggio in Giappone", "itinerario", "itinerario di viaggio", "primavera", "spring holiday", "hanami", "sakura", "holiday", "Asia"],
          "entities": ["Kyoto", "Tokyo", "Shinkansen", "Maruyama Park", "Shinjuku"], "decisions": ["stay in Shinjuku"]}),
    conv("gdpr_policy", "Privacy policy for our newsletter", "agent",
         ["Draft a privacy policy for our newsletter", "Mention GDPR consent, the data controller and the right to erasure.", "Add the retention period.",
          "Keep subscriber addresses for two years after the last open."],
         {"summary": ["A privacy policy for the newsletter.", "Consent, controller, erasure right, two-year retention."],
          "topics": ["privacy notice", "data protection", "mailing list"],
          "also_called": ["RGPD", "DSGVO", "privacy notice", "legal notice", "informativa privacy", "informativa", "protezione dei dati", "mailing list", "subscribers"],
          "entities": ["GDPR"], "decisions": ["keep addresses two years after the last open"]}),
    conv("python_perf", "Slow pandas groupby", "agent",
         ["My pandas groupby is slow on 5 million rows", "Use categorical dtypes and avoid apply; polars is faster still.", "How much faster?", "Roughly five times on this size."],
         {"summary": ["A slow groupby on five million rows.", "Categorical dtypes, no apply; polars about five times faster."],
          "topics": ["performance", "tables of data", "polars"],
          "also_called": ["dataframe", "dataframe operations", "speed up", "optimisation", "ottimizzazione", "lento", "tabelle grandi", "slow code"],
          "entities": ["pandas", "polars"], "decisions": ["move the heavy step to polars"]}),
    conv("wedding_speech", "Discorso per il matrimonio di mia sorella", "chat",
         ["Aiutami a scrivere il discorso per il matrimonio di mia sorella", "Inizia con un aneddoto d'infanzia e finisci con un brindisi.", "Può essere breve?",
          "Tre minuti sono perfetti."],
         {"summary": ["Un discorso per le nozze della sorella.", "Aneddoto d'infanzia, brindisi finale, tre minuti."],
          "topics": ["discorso", "nozze", "famiglia"],
          "also_called": ["wedding toast", "toast", "marriage", "sister", "best man speech", "bride", "wedding speech", "brindisi"],
          "entities": [], "decisions": ["tre minuti, finale con brindisi"]}),
    conv("garden_roses", "Potatura delle rose", "chat",
         ["Quando poto le rose?", "A fine inverno, prima che escano le gemme; taglia sopra un nodo rivolto all'esterno.", "E quelle rampicanti?",
          "Dopo la fioritura, lasciando tre o quattro rami principali."],
         {"summary": ["Quando potare le rose.", "Fine inverno, taglio sopra una gemma esterna; le rampicanti dopo la fioritura."],
          "topics": ["potatura", "giardinaggio", "rose"],
          "also_called": ["pruning", "pruning roses", "pruning flowering shrubs", "flowering shrubs", "gardening", "winter", "rose bushes", "giardino"],
          "entities": [], "decisions": ["potare a fine inverno"]}),
]

QUESTIONS: List[Dict[str, str]] = [
    {"id": "abbr_eu", "kind": "abbreviation", "text": "next EU summit dates", "answer": "eu_events"},
    {"id": "abbr_rgpd", "kind": "abbreviation", "text": "RGPD notice for subscribers", "answer": "gdpr_policy"},
    {"id": "abbr_irpef", "kind": "abbreviation", "text": "IRPEF refund", "answer": "tax_return"},
    {"id": "abbr_rtx", "kind": "abbreviation", "text": "RTX compile settings", "answer": "kernel_build"},
    {"id": "lang_mutuo", "kind": "language", "text": "mutuo tasso fisso o variabile", "answer": "mortgage"},
    {"id": "lang_giappone", "kind": "language", "text": "itinerario viaggio Giappone primavera", "answer": "trip_japan"},
    {"id": "lang_medical", "kind": "language", "text": "medical expenses tax return", "answer": "tax_return"},
    {"id": "lang_wedding", "kind": "language", "text": "toast for my sister's marriage", "answer": "wedding_speech"},
    {"id": "lang_guitar", "kind": "language", "text": "how to practise guitar chords", "answer": "guitar_lesson"},
    {"id": "lang_garden", "kind": "language", "text": "pruning flowering shrubs in winter", "answer": "garden_roses"},
    {"id": "para_pasta", "kind": "paraphrase", "text": "egg-based Roman dish recipe", "answer": "pasta_recipe"},
    {"id": "para_mobile", "kind": "paraphrase", "text": "mobile app that would not launch", "answer": "flutter_app"},
    {"id": "para_dataframe", "kind": "paraphrase", "text": "speed up dataframe operations", "answer": "python_perf"},
    {"id": "para_calendar", "kind": "paraphrase", "text": "European Union calendar of meetings", "answer": "eu_events"},
    {"id": "para_notice", "kind": "paraphrase", "text": "mailing list legal notice", "answer": "gdpr_policy"},
    {"id": "para_gpu", "kind": "paraphrase", "text": "GPU compiler options for the newest NVIDIA architecture", "answer": "kernel_build"},
]


def messages_of(conversation: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": text} for i, text in enumerate(conversation["turns"])]
