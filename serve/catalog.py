"""The models the Download panel offers: Qwen3.8-Flash-Next as ready .basalt files or as GGUF sets to convert.

Every entry was read from its repository at a pinned revision (2026-10-17). The sizes here are for display only: a
download is planned from the sizes and SHA-256 that Hugging Face reports for that revision (serve/downloads.py).
`convert`: "ready" a .basalt that loads as it is; "ok" a GGUF set the converter and the engine's kernels accept;
"untested" accepted by the lists in serve/models.py but never run through Basalt; "no" not offered for download.
The quality numbers say who measured them and against what: the references differ and must not be mixed."""
from __future__ import annotations

REVIEWED = "2026-10-17"
GIB = 2 ** 30

LICENSES = {
    "qwen": {
        "name": "Qwen Community License 1.0",
        "url": "https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE",
        "en": "Use, change and share are free. A product with over 100 million monthly users or over US$20 million of "
              "monthly revenue must show the model's name in its interface, and a Model-as-a-Service or AI work "
              "assistant offering needs a separate license from Qwen. Some repositories tag themselves Apache-2.0; "
              "the base model's license is the one above, so it is the one that applies here. This is a summary, "
              "not legal advice: the license text governs.",
        "it": "Uso, modifica e condivisione sono liberi. Un prodotto con oltre 100 milioni di utenti mensili o oltre "
              "20 milioni di dollari di ricavi mensili deve mostrare il nome del modello nell'interfaccia, e chi "
              "offre un servizio Model-as-a-Service o un assistente di lavoro AI ha bisogno di una licenza separata "
              "da Qwen. Alcuni repository si dichiarano Apache-2.0; la licenza del modello base è quella sopra, "
              "quindi è quella che vale qui. È un riassunto, non una consulenza legale: fa fede il testo.",
    },
    "swift": {
        "name": "Swift Open License 1.0 + Qwen Community License 1.0",
        "url": "https://huggingface.co/ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF/blob/main/LICENSE",
        "en": "Free for individuals and for organizations whose yearly gross revenue, affiliates included, is at most "
              "US$1,000,000; above that a Swift Enterprise License is needed. The Qwen Community License 1.0 "
              "(Qwen's terms on products, Model-as-a-Service and AI work assistants) applies as well. This is a "
              "summary, not legal advice: the license texts govern.",
        "it": "Gratuita per le persone e per le organizzazioni con ricavi lordi annui, affiliate comprese, fino a "
              "1.000.000 di dollari; oltre serve una Swift Enterprise License. Vale anche la Qwen Community License "
              "1.0 (le condizioni di Qwen su prodotti, Model-as-a-Service e assistenti di lavoro AI). È un "
              "riassunto, non una consulenza legale: fanno fede i testi delle licenze.",
    },
}

UNCENSORED_WARNING = {
    "en": "Safety training was removed from this model: it answers requests the original refuses, including harmful "
          "ones. What it writes and what you do with it are your responsibility.",
    "it": "Dal modello è stato tolto l'addestramento di sicurezza: risponde anche alle richieste che l'originale "
          "rifiuta, comprese quelle dannose. Ciò che scrive e come lo usi sono una tua responsabilità.",
}

_PLAIN = {
    "en": "The same model compressed to fewer bits per weight: a smaller file and faster answers, a little less "
          "faithful to the original (about 330 GB at full precision).",
    "it": "Lo stesso modello compresso a meno bit per peso: file più piccolo e risposte più rapide, un po' meno fedele "
          "all'originale (circa 330 GB a piena precisione).",
}

MMPROJ_NOTE = {
    "en": "The download includes the vision projector (about 0.9 GB), which the conversion puts inside the .basalt "
          "file so the model can read images.",
    "it": "Il download comprende il proiettore di visione (circa 0,9 GB), che la conversione mette dentro il file "
          ".basalt perché il modello possa leggere le immagini.",
}
CONVERT_NOTE = {
    "en": "These are GGUF files: after the download the model appears in the list as “To convert”; the conversion "
          "needs about as much free disk space again as the download, and the GGUF folder can be deleted afterwards.",
    "it": "Sono file GGUF: dopo il download il modello compare nell'elenco come «Da convertire»; la conversione "
          "richiede altrettanto spazio libero su disco e la cartella dei GGUF si può poi cancellare.",
}
READY_NOTE = {
    "en": "This is a ready .basalt file: no conversion, it loads as soon as the download ends.",
    "it": "È un file .basalt già pronto: nessuna conversione, si carica appena il download finisce.",
}
UNTESTED_NOTE = {
    "en": "Its expert formats are accepted by Basalt's lists, but this file has not been run through Basalt.",
    "it": "I formati degli esperti sono accettati dagli elenchi di Basalt, ma questo file non è mai stato provato "
          "con Basalt.",
}

# ---- who measured what ---------------------------------------------------------------------------------------
# Basalt's own table: next-token agreement with Q8_0 through the decode path, 20 pieces of 512 tokens of wikitext-2
# (README.md, "Quality"); KLD against Q8_0.
BASALT_Q8 = {"q2_0": (80.6, 0.486), "iq3_xxs": (86.8, 0.277), "iq3_s": (89.4, 0.177), "ud-q2_k_xl": (84.3, 0.323),
             "ud-iq3_xxs": (87.0, 0.244), "ud-q3_k_xl": (89.3, 0.160), "ud-q4_k_xl": (93.0, 0.068),
             "coder": (69.0, 1.03)}


def _basalt(key: str) -> dict:
    top, kld = BASALT_Q8[key]
    return {"en": f"{top:g}% of the next tokens equal Q8_0's (Basalt's own measurement on wikitext-2; KLD {kld:g})",
            "it": f"{str(top).replace('.', ',')}% dei token successivi uguali a Q8_0 (misura di Basalt su wikitext-2; "
                  f"KLD {str(kld).replace('.', ',')})",
            "short": {"en": f"{top:g}% top token = Q8_0", "it": f"{str(top).replace('.', ',')}% token = Q8_0"},
            "source": "basalt"}


def _strata(top: float, kl: float) -> dict:
    return {"en": f"{top:g}% of the next tokens equal Q8_0's (Strata's measurement on code, prose and other "
                  f"languages; KL {kl:g}). Not measured by Basalt.",
            "it": f"{str(top).replace('.', ',')}% dei token successivi uguali a Q8_0 (misura di Strata su codice, "
                  f"prosa e altre lingue; KL {str(kl).replace('.', ',')}). Non misurato da Basalt.",
            "short": {"en": f"{top:g}% top token = Q8_0 (Strata)", "it": f"{str(top).replace('.', ',')}% token = Q8_0 (Strata)"},
            "source": "strata"}


def _authors(en: str, it: str, short_en: str, short_it: str) -> dict:
    return {"en": en + " Declared by the authors, not reproduced here.",
            "it": it + " Dichiarato dagli autori, non riprodotto qui.",
            "short": {"en": short_en, "it": short_it}, "source": "authors"}


_NONE = {"en": "No fidelity figure is published for this file.", "it": "Per questo file non è pubblicata alcuna misura "
         "di fedeltà.", "short": {"en": "no fidelity figure", "it": "nessuna misura di fedeltà"}, "source": "none"}

# ---- the entries -----------------------------------------------------------------------------------------------
_E: list[dict] = []


def _add(**kw) -> None:
    kw.setdefault("uncensored", False)
    kw.setdefault("gated", False)
    kw.setdefault("license", "qwen")
    kw.setdefault("notes", [])
    kw.setdefault("match", {"all": [], "none": []})
    _E.append(kw)


def _shards(prefix: str, n: int, folder: str = "") -> list[str]:
    return [f"{folder}{prefix}-{i:05d}-of-{n:05d}.gguf" for i in range(1, n + 1)]


# -- ready .basalt files (jesdga/Qwen3.8-Flash-Next-Basalt) --
_JD = dict(repo="jesdga/Qwen3.8-Flash-Next-Basalt", revision="2a6a758dc12f1d027d2d35b8964110df7e80f70b",
           kind="basalt", convert="ready", folder="jesdga-basalt", family="base")
_add(id="basalt-q2_0", name="Qwen3.8-Flash-Next · Q2_0", quant="Q2_0", files=["Qwen3.8-FN-GSQ-RCO-Q2_0.basalt"],
     approx_bytes=69_690_000_000, fidelity=_basalt("q2_0"), match={"all": ["fn-gsq-rco-q2_0"], "none": []},
     short={"en": "Smallest and fastest ready file: 2.4 bits per weight on average. The most compressed, so the "
                  "least faithful.",
            "it": "Il file pronto più piccolo e veloce: 2,4 bit per peso in media. Il più compresso, quindi il meno "
                  "fedele."},
     long={"en": "The base model compressed to about 2.4 bits per weight by ISTA-DASLab's GSQ-RCO method (a bit width "
                 "chosen per tensor under a size budget), already converted to Basalt's format with a drafter "
                 "tuned for it. The authors report an average of 89.07 over their reasoning tasks against 93.12 for "
                 "the full-precision model. Pick it when memory or speed matter most.",
           "it": "Il modello base compresso a circa 2,4 bit per peso con il metodo GSQ-RCO di ISTA-DASLab (larghezza "
                 "di bit scelta per tensore entro un budget di dimensione), già convertito nel formato di Basalt con "
                 "un drafter tarato per questo file. Gli autori riportano una media di 89,07 sui loro compiti di "
                 "ragionamento contro 93,12 del modello a piena precisione. Scegli questo file quando contano "
                 "memoria e velocità."}, **_JD)
_add(id="basalt-iq3_xxs", name="Qwen3.8-Flash-Next · IQ3_XXS", quant="IQ3_XXS",
     files=["Qwen3.8-FN-GSQ-RCO-IQ3_XXS.basalt"], approx_bytes=79_870_000_000, fidelity=_basalt("iq3_xxs"),
     match={"all": ["fn-gsq-rco-iq3_xxs"], "none": []},
     short={"en": "3 bits per weight: the middle of the ready files, a good balance of size, speed and fidelity.",
            "it": "3 bit per peso: il centro dei file pronti, un buon equilibrio tra dimensione, velocità e "
                  "fedeltà."},
     long={"en": "The base model at about 3 bits per weight (ISTA-DASLab GSQ-RCO), already converted to Basalt's "
                 "format with a tuned drafter. The authors report 92.57 on their reasoning tasks against 93.12 at "
                 "full precision. Basalt measures 86.8% of next tokens equal to Q8_0's.",
           "it": "Il modello base a circa 3 bit per peso (GSQ-RCO di ISTA-DASLab), già convertito nel formato di "
                 "Basalt con un drafter tarato. Gli autori riportano 92,57 sui loro compiti di ragionamento contro "
                 "93,12 a piena precisione. Basalt misura l'86,8% di token successivi uguali a Q8_0."}, **_JD)
_add(id="basalt-iq3_s", name="Qwen3.8-Flash-Next · IQ3_S", quant="IQ3_S", files=["Qwen3.8-FN-GSQ-RCO-IQ3_S.basalt"],
     approx_bytes=86_930_000_000, fidelity=_basalt("iq3_s"), match={"all": ["fn-gsq-rco-iq3_s"], "none": []},
     short={"en": "3.5 bits per weight: the most faithful of the 3-bit files, a little slower and larger.",
            "it": "3,5 bit per peso: il più fedele tra i file a 3 bit, un po' più lento e grande."},
     long={"en": "The base model at about 3.5 bits per weight (ISTA-DASLab GSQ-RCO), already converted to Basalt's "
                 "format with a tuned drafter. The authors report 93.26 on their reasoning tasks against 93.12 at "
                 "full precision (a difference within the noise of the tests). Basalt measures 89.4% of next tokens "
                 "equal to Q8_0's.",
           "it": "Il modello base a circa 3,5 bit per peso (GSQ-RCO di ISTA-DASLab), già convertito nel formato di "
                 "Basalt con un drafter tarato. Gli autori riportano 93,26 sui loro compiti di ragionamento contro "
                 "93,12 a piena precisione (una differenza entro il rumore dei test). Basalt misura l'89,4% di "
                 "token successivi uguali a Q8_0."}, **_JD)
_add(id="basalt-ud-q4_k_xl", name="Qwen3.8-Flash-Next · UD-Q4_K_XL (Unsloth)", quant="UD-Q4_K_XL",
     files=["Qwen3.8-FN-UD-Q4_K_XL.basalt"], approx_bytes=114_600_000_000, fidelity=_basalt("ud-q4_k_xl"),
     match={"all": ["fn-ud-q4_k_xl"], "none": []},
     short={"en": "4-bit class (Unsloth's dynamic quantization): close to the full model, but very large.",
            "it": "Classe a 4 bit (quantizzazione dinamica di Unsloth): vicino al modello pieno, ma molto grande."},
     long={"en": "Unsloth's 4-bit-class dynamic quantization, already converted to Basalt's format. It is the most faithful "
                 "of the ready files that most PCs can still hold: Basalt measures 93.0% of next tokens equal to "
                 "Q8_0's. Needs far more memory than the 3-bit files.",
           "it": "La quantizzazione dinamica di Unsloth (classe a 4 bit), già convertita nel formato di Basalt. È il più "
                 "fedele tra i file pronti che molti PC riescono ancora a contenere: Basalt misura il 93,0% di "
                 "token successivi uguali a Q8_0. Richiede molta più memoria dei file a 3 bit."}, **_JD)
_add(id="basalt-q8_0", name="Qwen3.8-Flash-Next · Q8_0", quant="Q8_0", files=["Qwen3.8-FN-Q8_0.basalt"],
     approx_bytes=191_490_000_000,
     fidelity={"en": "The reference the other measurements compare to.", "it": "È il riferimento con cui si "
               "confrontano le altre misure.", "short": {"en": "the Basalt reference", "it": "il riferimento di Basalt"},
               "source": "basalt"},
     match={"all": ["flash-next-q8_0"], "none": ["uncensored", "abliterated", "swift"]},
     short={"en": "8 bits per weight: the closest to the full model, but the largest file. The reference for the other files.",
            "it": "8 bit per peso: il più vicino al modello pieno, ma il file più grande. Il riferimento per gli altri file."},
     long={"en": "The base model at 8 bits per weight, already converted to Basalt's format. It is the reference "
                 "against which Basalt measures the smaller files; it needs about 180 GB of disk and a lot of memory.",
           "it": "Il modello base a 8 bit per peso, già convertito nel formato di Basalt. È il riferimento con cui "
                 "Basalt misura i file più piccoli; richiede circa 180 GB di disco e molta memoria."}, **_JD)

# -- ISTA-DASLab GGUF: IQ2_XS and the Coder --
_IS = dict(repo="ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF", revision="ed59f92082b1e93c0e96d60a8b11aab089b52f09",
           kind="gguf", folder="ista-gsq-rco", family="base")
_add(id="ista-iq2_xs", name="Qwen3.8-Flash-Next · IQ2_XS", quant="IQ2_XS", convert="untested",
     files=_shards("IQ2_XS/Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS", 2) + ["mmproj-Qwen3.8-Flash-Next-BF16.gguf"],
     approx_bytes=68_940_000_000,
     fidelity=_authors("On its reasoning tasks it averages 89.16 against 93.12 for the full-precision model.",
                       "Sui suoi compiti di ragionamento ottiene in media 89,16 contro 93,12 del modello a piena "
                       "precisione.", "89.16 vs 93.12 full precision (authors)", "89,16 vs 93,12 a piena precisione "
                       "(autori)"),
     match={"all": ["gsq-rco-iq2_xs"], "none": ["abliterated", "swift", "uncensored"]},
     notes=[UNTESTED_NOTE],
     short={"en": "2.5 bits per weight, between Q2_0 and IQ3_XXS in size. To convert; not run through Basalt yet.",
            "it": "2,5 bit per peso, tra Q2_0 e IQ3_XXS per dimensione. Da convertire; non ancora provato con "
                  "Basalt."},
     long={"en": "The base model at about 2.5 bits per weight (ISTA-DASLab GSQ-RCO). There is no ready Basalt file "
                 "for this size, so it is downloaded as GGUF and converted here. The authors report 89.16 on their "
                 "reasoning tasks against 93.12 at full precision; Basalt has no measurement of its own for it.",
           "it": "Il modello base a circa 2,5 bit per peso (GSQ-RCO di ISTA-DASLab). Per questa taglia non c'è un "
                 "file Basalt pronto: si scarica come GGUF e si converte qui. Gli autori riportano 89,16 sui loro "
                 "compiti di ragionamento contro 93,12 a piena precisione; Basalt non ha una misura propria."}, **_IS)
_add(id="coder-iq1_m", name="Qwen3.8-Flash-Next Coder · IQ1_M", quant="IQ1_M", convert="ok", family="coder",
     repo="ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF", revision="5348543e0147355ac9cbcb031184a3546350988e",
     kind="gguf", folder="ista-gsq-rco-coder",
     files=_shards("IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M", 2) + ["mmproj-Qwen3.8-Flash-Next-BF16.gguf"],
     approx_bytes=59_320_000_000, fidelity=_basalt("coder"),
     match={"all": ["gsq-rco-iq1_m"], "none": ["abliterated", "swift"]},
     short={"en": "A coding version: half of the experts removed, so it is the smallest file. Strong at code, weaker "
                  "elsewhere.",
            "it": "Una versione per il codice: metà degli esperti tolti, quindi il file più piccolo. Forte sul "
                  "codice, più debole altrove."},
     long={"en": "ISTA-DASLab's coding version: 256 of the 512 experts in each layer were removed, chosen on code, "
                 "agent and image data, and the rest are stored at about 3.5 bits. The authors report 91.3% of the "
                 "full model's score on SWE-bench Verified (75.60 against 82.80) and 98.7% on LiveCodeBench. It is "
                 "weaker outside code and in languages other than English (Chinese answers have been reported "
                 "wrong or looping). Basalt measures only 69% of next tokens equal to Q8_0's, which says how far it "
                 "is from the full model on general text.",
           "it": "La versione per il codice di ISTA-DASLab: in ogni strato 256 esperti su 512 sono stati tolti, scelti "
                 "su dati di codice, agenti e immagini, e gli altri sono salvati a circa 3,5 bit. Gli autori "
                 "riportano il 91,3% del punteggio del modello pieno su SWE-bench Verified (75,60 contro 82,80) e il "
                 "98,7% su LiveCodeBench. È più debole fuori dal codice e nelle lingue diverse dall'inglese (sono "
                 "state segnalate risposte in cinese sbagliate o in loop). Basalt misura solo il 69% di token "
                 "successivi uguali a Q8_0, il che dice quanto è lontano dal modello pieno sul testo generale."})

# -- Swift 1.5 (ukisai): same structure as ISTA, fine-tuned to think shorter --
_SW = dict(repo="ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF", revision="99bb8f7f95c7aa7b24a36a7786a4f657b30f5d3d",
           kind="gguf", folder="swift-1.5", family="swift", license="swift", convert="untested",
           notes=[UNTESTED_NOTE])
_SWIFT_KLD = {"q2_0": 0.4244, "iq2_xs": 0.3413, "iq3_xxs": 0.2401, "iq3_s": 0.1447}
_SWIFT_BYTES = {"q2_0": 66_550_000_000, "iq2_xs": 68_150_000_000, "iq3_xxs": 75_970_000_000, "iq3_s": 83_740_000_000}
for _q in ("Q2_0", "IQ2_XS", "IQ3_XXS", "IQ3_S"):
    _k = _q.lower()
    _kld = _SWIFT_KLD[_k]
    _add(id=f"swift-{_k}", name=f"Swift 1.5 · {_q}", quant=_q,
         files=_shards(f"Swift-Qwen3.8-Flash-Next-GSQ-RCO-{_q}", 2) + ["mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf"],
         approx_bytes=_SWIFT_BYTES[_k] + 907_543_520,
         fidelity={"en": f"KLD {_kld:g} against Swift's own full-precision model (measured by its author on 100 pieces "
                         "of 512 tokens). Not comparable with the other files' figures.",
                   "it": f"KLD {str(_kld).replace('.', ',')} rispetto al modello a piena precisione di Swift stesso "
                         "(misurato dall'autore su 100 pezzi da 512 token). Non confrontabile con le misure degli "
                         "altri file.",
                   "short": {"en": f"KLD {_kld:g} vs Swift's own BF16", "it": f"KLD {str(_kld).replace('.', ',')} vs "
                                                                            "il BF16 di Swift"},
                   "source": "authors"},
         match={"all": ["swift", _k.replace("_", "_")], "none": ["abliterated", "uncensored"]},
         short={"en": "A fine-tune of the base that thinks much less before answering (about 63% fewer thinking "
                      f"tokens, declared). {_q} compression.",
                "it": "Un fine-tune del modello base che pensa molto meno prima di rispondere (circa il 63% di "
                      f"token di ragionamento in meno, dichiarato). Compressione {_q}."},
         long={"en": "Swift 1.5 by UkisAI is the base model fine-tuned to reason with far fewer tokens before it "
                     "answers: the author declares 63.4% fewer thinking tokens, answers about 1.8 times sooner and "
                     "under 1% loss of accuracy, which is not the same model as the base. The file is the same "
                     f"size and speed per token as the matching ISTA file; this one is {_q}. Its quality figure is "
                     "measured against Swift's own full-precision model, not against Qwen's. "
                     + ("The author marks Q2_0 as experimental. " if _q == "Q2_0" else ""),
               "it": "Swift 1.5 di UkisAI è il modello base con un fine-tuning per ragionare con molti meno token "
                     "prima di rispondere: l'autore dichiara il 63,4% di token di ragionamento in meno, risposte "
                     "circa 1,8 volte prima e meno dell'1% di perdita di accuratezza; non è lo stesso modello della "
                     f"base. Il file ha la stessa dimensione e velocità per token del file ISTA corrispondente; "
                     f"questo è {_q}. La misura di qualità è rispetto al modello a piena precisione di Swift, non a "
                     "quello di Qwen. "
                     + ("L'autore indica Q2_0 come sperimentale. " if _q == "Q2_0" else "")}, **_SW)

# -- Unsloth dynamic quantizations --
_UN = dict(repo="unsloth/Qwen3.8-Flash-Next-GGUF", revision="766911a6b7369840a91dbcd95f9f997acaab6cd6", kind="gguf",
           folder="unsloth", family="unsloth")
_UD = [("UD-Q2_K_XL", "ud-q2_k_xl", 3, 78_870_000_000, "ok", _basalt("ud-q2_k_xl"), "2-bit class", "classe a 2 bit"),
       ("UD-IQ3_XXS", "ud-iq3_xxs", 3, 81_960_000_000, "ok", _basalt("ud-iq3_xxs"), "3-bit class", "classe a 3 bit"),
       ("UD-Q3_K_XL", "ud-q3_k_xl", 3, 89_990_000_000, "ok", _basalt("ud-q3_k_xl"), "3-bit class", "classe a 3 bit"),
       ("UD-IQ4_XS", "ud-iq4_xs", 3, 93_680_000_000, "untested", _strata(88.9, 0.101), "4-bit class",
        "classe a 4 bit")]
for _q, _k, _n, _b, _cv, _fid, _bits_en, _bits_it in _UD:
    _add(id=f"unsloth-{_k}", name=f"Qwen3.8-Flash-Next · {_q} (Unsloth)", quant=_q, convert=_cv,
         files=_shards(f"{_q}/Qwen3.8-Flash-Next-{_q}", _n) + ["mmproj-BF16.gguf"], approx_bytes=_b + 907_542_944,
         fidelity=_fid, match={"all": ["flash-next-" + _k], "none": ["uncensored", "abliterated", "swift"]},
         notes=[UNTESTED_NOTE] if _cv == "untested" else [],
         short={"en": f"Unsloth's dynamic quantization ({_bits_en}): it keeps the sensitive tensors at more bits than "
                      "the rest.",
                "it": f"Quantizzazione dinamica di Unsloth ({_bits_it}): tiene i tensori sensibili a più bit del "
                      "resto."},
         long={"en": f"Unsloth's “UD” (dynamic) quantization of the base model ({_bits_en}): the tensors that suffer "
                     "most from compression keep more bits, the others fewer. It is the same model as the base, only "
                     "compressed differently from the ISTA files.",
               "it": f"La quantizzazione «UD» (dinamica) di Unsloth del modello base ({_bits_it}): i tensori che "
                     "soffrono di più la compressione tengono più bit, gli altri meno. È lo stesso modello della "
                     "base, solo compresso in modo diverso dai file ISTA."}, **_UN)

# -- uncensored: SC117 (ISTA structure with OrcaRouter's abliteration grafted in) --
_SC = dict(repo="SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF", revision="eec4e10a2c29b440a2ae85591fa11569ce752963",
           kind="gguf", folder="sc117-abliterated", family="uncensored", uncensored=True, convert="untested")
_SC_BYTES = {"Q2_0": 66_820_000_000, "IQ2_XS": 68_470_000_000, "IQ3_XXS": 76_140_000_000, "IQ3_S": 83_860_000_000}
for _q in ("Q2_0", "IQ2_XS", "IQ3_XXS", "IQ3_S"):
    _add(id=f"sc117-{_q.lower()}", name=f"Uncensored (SC117) · {_q}", quant=_q,
         files=_shards(f"{_q}/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-{_q}", 2)
         + ["mmproj-Qwen3.8-Flash-Next-BF16.gguf"], approx_bytes=_SC_BYTES[_q] + 907_543_000, fidelity=_NONE,
         match={"all": ["gsq-rco-abliterated", _q.lower()], "none": ["swift"]},
         notes=[UNCENSORED_WARNING, UNTESTED_NOTE],
         short={"en": f"Uncensored: the ISTA {_q} file with the abliteration of OrcaRouter's model grafted in. Same "
                      "size as the base file. No fidelity figure published.",
                "it": f"Uncensored: il file ISTA {_q} con l'abliterazione del modello di OrcaRouter innestata. Stessa "
                      "dimensione del file base. Nessuna misura di fedeltà pubblicata."},
         long={"en": f"Made by SC117: the ISTA GSQ-RCO {_q} file in which 144 of its 1,223 tensors were replaced "
                     "byte for byte with the ones from OrcaRouter's abliterated model (the refusal direction removed "
                     "from the weights), about 0.25 GB larger. The quantization values were not recomputed, so how "
                     "much it differs from the ISTA file in quality has not been measured by anyone: no KLD is "
                     "published. Same size and speed as the base file of the same name.",
               "it": f"Fatto da SC117: il file ISTA GSQ-RCO {_q} in cui 144 dei suoi 1.223 tensori sono stati "
                     "sostituiti byte per byte con quelli del modello abliterato di OrcaRouter (la direzione del "
                     "rifiuto tolta dai pesi), circa 0,25 GB più grande. I valori di quantizzazione non sono stati "
                     "ricalcolati, quindi nessuno ha misurato di quanto la qualità differisca dal file ISTA: non "
                     "c'è alcun KLD pubblicato. Stessa dimensione e velocità del file base con lo stesso nome."},
         **_SC)

# -- uncensored: OrcaRouter (gated) --
_OR = dict(repo="orcarouter/Qwen3.8-Flash-Next-Uncensored-GGUF", revision="d27c0a15447dd92ae7c90919352c6810496945b1",
           kind="gguf", folder="orcarouter-uncensored", family="uncensored", uncensored=True, gated=True)
_OR_FID = {"en": "No KLD or perplexity is published for the GGUF files. The author declares refusals falling from "
                 "64-100% to 0-3.3% and MMLU-Pro from 77.8 to 76.2 (measured on the full-precision model).",
           "it": "Per i file GGUF non sono pubblicati KLD né perplessità. L'autore dichiara rifiuti dal 64-100% "
                 "allo 0-3,3% e MMLU-Pro da 77,8 a 76,2 (misurati sul modello a piena precisione).",
           "short": {"en": "refusals 64-100% → 0-3.3% (author)", "it": "rifiuti 64-100% → 0-3,3% (autore)"},
           "source": "authors"}
_OR_LONG = {"en": "OrcaRouter's uncensored model: the base with a single “refusal direction” (found at layer 24) "
                  "removed from 149 tensors, so it answers what the original would refuse. ",
            "it": "Il modello uncensored di OrcaRouter: la base con una sola «direzione del rifiuto» (trovata allo "
                  "strato 24) tolta da 149 tensori, così risponde a ciò che l'originale rifiuterebbe. "}
_OR_GATE = {"en": "The repository is gated: you need a Hugging Face account, to accept its terms on its page and a "
                  "token. The download panel guides you through it.",
            "it": "Il repository è ad accesso controllato: servono un account Hugging Face, l'accettazione dei "
                  "termini sulla sua pagina e un token. Il pannello di download ti guida."}
_add(id="orca-iq3_xxs", name="Uncensored (OrcaRouter) · IQ3_XXS", quant="IQ3_XXS", convert="ok", fidelity=_OR_FID,
     files=_shards("Qwen3.8-Flash-Next-Uncensored-IQ3_XXS", 2) + ["mmproj-Qwen3.8-Flash-Next-Uncensored-F16.gguf"],
     approx_bytes=86_110_000_000, match={"all": ["uncensored", "iq3_xxs"], "none": ["abliterated", "swift"]},
     notes=[UNCENSORED_WARNING, _OR_GATE],
     short={"en": "Uncensored at 3 bits per weight. The file this PC's current model was converted from. Gated "
                  "repository.",
            "it": "Uncensored a 3 bit per peso. Il file da cui è stato convertito il modello attuale di questo PC. "
                  "Repository ad accesso controllato."},
     long={"en": _OR_LONG["en"] + "This IQ3_XXS file converts with Basalt (it needs the flag that rounds a few small "
                                  "tensors to BF16, which the conversion adds by itself). The files total about 80 GB, and the "
                                  "second shard is mostly the per-layer embedding table.",
           "it": _OR_LONG["it"] + "Questo file IQ3_XXS si converte con Basalt (serve il flag che arrotonda a BF16 "
                                  "alcuni tensori piccoli, che la conversione aggiunge da sola). I file pesano circa 80 GB in tutto e "
                                  "il secondo shard è soprattutto la tabella degli embedding per strato."}, **_OR)
_add(id="orca-iq4_xs", name="Uncensored (OrcaRouter) · IQ4_XS", quant="IQ4_XS", convert="untested", fidelity=_OR_FID,
     files=_shards("Qwen3.8-Flash-Next-Uncensored-IQ4_XS", 3) + ["mmproj-Qwen3.8-Flash-Next-Uncensored-F16.gguf"],
     approx_bytes=98_380_000_000, match={"all": ["uncensored", "iq4_xs"], "none": ["abliterated", "swift"]},
     notes=[UNCENSORED_WARNING, _OR_GATE, UNTESTED_NOTE],
     short={"en": "Uncensored, IQ4_XS: a step up from IQ3_XXS in size and fidelity. Gated.",
            "it": "Uncensored, IQ4_XS: un passo avanti rispetto a IQ3_XXS per dimensione e fedeltà. Ad "
                  "accesso controllato."},
     long={"en": _OR_LONG["en"] + "This IQ4_XS file has expert formats Basalt accepts, but it has not been tried.",
           "it": _OR_LONG["it"] + "Questo file IQ4_XS ha formati degli esperti che Basalt accetta, ma non è mai stato "
                                  "provato."}, **_OR)
_add(id="orca-q8_0", name="Uncensored (OrcaRouter) · Q8_0", quant="Q8_0", convert="ok", fidelity=_OR_FID,
     files=_shards("Qwen3.8-Flash-Next-Uncensored-Q8_0", 5) + ["mmproj-Qwen3.8-Flash-Next-Uncensored-F16.gguf"],
     approx_bytes=189_120_000_000, match={"all": ["uncensored", "q8_0"], "none": ["abliterated", "swift", "mtp"]},
     notes=[UNCENSORED_WARNING, _OR_GATE],
     short={"en": "Uncensored at 8 bits per weight: the closest to the full model. Gated.",
            "it": "Uncensored a 8 bit per peso: il più vicino al modello pieno. Ad accesso controllato."},
     long={"en": _OR_LONG["en"] + "At 8 bits per weight the file is practically the full model minus the refusals; "
                                  "it needs about 175 GB of disk and a lot of memory.",
           "it": _OR_LONG["it"] + "A 8 bit per peso il file è praticamente il modello pieno meno i rifiuti; richiede "
                                  "circa 175 GB di disco e molta memoria."}, **_OR)

# -- uncensored: a ready .basalt by a third party --
_add(id="jsonai-q4_k_m", name="Uncensored (JSon-AI) · Q4_K_M, ready .basalt", quant="Q4_K_M", kind="basalt",
     convert="ready", family="uncensored", uncensored=True, repo="JSon-AI/Qwen3.8-Flash-Next-Basalt-Uncensored",
     revision="0c12d194c98c10d730cc1109f32272cf0c234486", folder="jsonai-basalt-uncensored",
     files=["Qwen3.8-FN-Uncensored-Q4_K_M-v2.basalt"], approx_bytes=122_470_000_000, fidelity=_NONE,
     match={"all": ["fn-uncensored-q4_k_m"], "none": []},
     notes=[UNCENSORED_WARNING,
            {"en": "Made by a third party, published on 2026-10-15 and not yet downloaded by anyone when this "
                   "list was written: its origin cannot be checked here beyond the SHA-256 the download verifies. "
                   "It was converted from OrcaRouter's Q4_K_M, whose 5-bit expert format is accepted by Basalt's "
                   "lists but not covered by its parity tests.",
             "it": "Fatto da terzi, pubblicato il 15/10/2026 e non ancora scaricato da nessuno quando questo elenco "
                   "è stato scritto: la sua origine non è verificabile qui oltre allo SHA-256 che il download "
                   "controlla. È stato convertito dal Q4_K_M di OrcaRouter, il cui formato degli esperti a 5 bit è "
                   "accettato dagli elenchi di Basalt ma non è coperto dai suoi test di parità."}],
     short={"en": "A ready .basalt of the uncensored model (Q4_K_M). Third-party file, not verified.",
            "it": "Un .basalt già pronto del modello uncensored (Q4_K_M). File di terzi, non verificato."},
     long={"en": "A .basalt file of OrcaRouter's uncensored model (Q4_K_M), converted by JSon-AI, so it needs no "
                 "conversion here. It needs a recent engine (it uses the Q4_K prompt kernels). No fidelity figure "
                 "is published, and the file comes from a single, new, third-party account.",
           "it": "Un file .basalt del modello uncensored di OrcaRouter (Q4_K_M), convertito da JSon-AI, quindi non "
                 "richiede conversione qui. Richiede un motore recente (usa i kernel Q4_K per il prompt). Non è "
                 "pubblicata alcuna misura di fedeltà e il file viene da un solo account di terzi, nuovo."})

# -- shown, not offered: why they would not run --
_NO_EXPERTS = {"en": "Basalt's kernels have no format for these experts, so the engine would refuse the model.",
               "it": "I kernel di Basalt non hanno un formato per questi esperti, quindi il motore rifiuterebbe il "
                     "modello."}
for _id, _name, _q, _gb, _why in (
        ("unsloth-ud-q5_k_xl", "Qwen3.8-Flash-Next · UD-Q5_K_XL (Unsloth)", "UD-Q5_K_XL", 158.29,
         {"en": "It stores part of the experts as Q6_K, which Basalt's kernels do not support.",
          "it": "Tiene una parte degli esperti in Q6_K, che i kernel di Basalt non supportano."}),
        ("unsloth-ud-q6_k_xl", "Qwen3.8-Flash-Next · UD-Q6_K_XL (Unsloth)", "UD-Q6_K_XL", 169.17,
         {"en": "Its experts are Q6_K, which Basalt's kernels do not support.",
          "it": "I suoi esperti sono Q6_K, che i kernel di Basalt non supportano."}),
        ("unsloth-ud-iq1_s", "Qwen3.8-Flash-Next · UD-IQ1_S (Unsloth)", "UD-IQ1_S", 72.55,
         {"en": "Its experts are IQ1_S, which Basalt's kernels do not support.",
          "it": "I suoi esperti sono IQ1_S, che i kernel di Basalt non supportano."}),
        ("orca-iq2_xxs", "Uncensored (OrcaRouter) · IQ2_XXS", "IQ2_XXS", 74.85,
         {"en": "Its first layers use Q4_0 experts, which Basalt's kernels do not support.",
          "it": "I suoi primi strati usano esperti Q4_0, che i kernel di Basalt non supportano."}),
        ("orca-k-quants", "Uncensored (OrcaRouter) · Q2_K and Q3_K", "Q2_K / Q3_K", 80.45,
         {"en": "K-quants of 2 and 3 bits use expert formats Basalt's kernels do not support.",
          "it": "I K-quant a 2 e 3 bit usano formati degli esperti che i kernel di Basalt non supportano."})):
    _add(id=_id, name=_name, quant=_q, convert="no", files=[], approx_bytes=int(_gb * 1e9), repo="", revision="",
         kind="gguf", folder="", family="uncensored" if _id.startswith("orca") else "unsloth",
         uncensored=_id.startswith("orca"), fidelity=_NONE, reason=_why,
         short={"en": "Not compatible with Basalt. " + _why["en"], "it": "Non compatibile con Basalt. " + _why["it"]},
         long={"en": "Not offered for download: " + _why["en"], "it": "Non offerto per il download: " + _why["it"]})
_add(id="qwen38-35b-a3b", name="Qwen3.8-35B-A3B (community distillation)", quant="—", convert="no", files=[],
     approx_bytes=0, repo="", revision="", kind="gguf", folder="", family="base", fidelity=_NONE,
     reason={"en": "It is a different architecture (Qwen3.6-35B-A3B: 40 layers, hidden size 2048, 256 experts) with "
                   "Qwen3.8 only as the teacher of a distillation. Basalt's engine is written for Flash-Next's "
                   "shapes (48 layers, 2560, 512 experts, hyper-connections), so supporting it would need a second "
                   "engine, not small changes.",
             "it": "È un'altra architettura (Qwen3.6-35B-A3B: 40 strati, dimensione 2048, 256 esperti) con Qwen3.8 "
                   "solo come insegnante di una distillazione. Il motore di Basalt è scritto per le forme di "
                   "Flash-Next (48 strati, 2560, 512 esperti, hyper-connection), quindi supportarlo richiederebbe "
                   "un secondo motore, non piccole modifiche."},
     short={"en": "Not compatible: another architecture; there is no official Qwen3.8-35B-A3B.",
            "it": "Non compatibile: un'altra architettura; non esiste un Qwen3.8-35B-A3B ufficiale."},
     long={"en": "There is no official Qwen3.8-35B-A3B. The models sold under that name are community "
                 "distillations of Qwen3.8 into the Qwen3.6-35B-A3B architecture, which Basalt cannot run.",
           "it": "Non esiste un Qwen3.8-35B-A3B ufficiale. I modelli venduti con quel nome sono distillazioni "
                 "della comunità di Qwen3.8 nell'architettura Qwen3.6-35B-A3B, che Basalt non può eseguire."})

CATALOG: list[dict] = _E
GENERIC = {"plain": _PLAIN, "mmproj": MMPROJ_NOTE, "convert": CONVERT_NOTE, "ready": READY_NOTE}
FAMILIES = {
    "base": {"en": "Base model", "it": "Modello base"},
    "coder": {"en": "Coder", "it": "Coder"},
    "swift": {"en": "Swift 1.5", "it": "Swift 1.5"},
    "unsloth": {"en": "Unsloth", "it": "Unsloth"},
    "uncensored": {"en": "Uncensored", "it": "Uncensored"},
}


def find(entry_id: str) -> dict | None:
    return next((e for e in CATALOG if e["id"] == entry_id), None)


def public(entry: dict) -> dict:
    """The entry as the page receives it (nothing in it is a secret)."""
    out = {k: entry[k] for k in ("id", "name", "family", "quant", "kind", "convert", "uncensored", "gated", "license",
                                 "approx_bytes", "short", "long", "fidelity", "notes", "repo", "folder") if k in entry}
    out["reason"] = entry.get("reason")
    out["downloadable"] = entry["convert"] != "no" and bool(entry["files"])
    out["page"] = f"https://huggingface.co/{entry['repo']}" if entry.get("repo") else None
    return out


def describe(name_or_path: str) -> dict | None:
    """The catalog entry a local file or model name corresponds to (by its name), or None."""
    low = (name_or_path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for e in CATALOG:
        m = e["match"]
        if m["all"] and all(s in low for s in m["all"]) and not any(s in low for s in m["none"]):
            return e
    return None
