"""Automated runner for the extended Jev System One test suite.
Executes Category 1, 2, 3 (short vs long), 4, and Batch Agglomerations (10 events)
against TypeSafe live API, recording probabilities, confidence, latency, and tokens.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

PLAN_CARD = {
    "titolo": "Weekend sulla neve a Cervinia",
    "date": "Venerdì 24 - Domenica 26 Ottobre",
    "destinazione": "Cervinia (Valle d'Aosta)",
    "partecipanti": ["Io (organizzatore)", "Gianni", "Erica", "Diana"],
    "logistica_e_prenotazioni": {
        "alloggio": "Hotel prenotato e confermato a Cervinia per 4 persone",
        "skipass": "Skipass già acquistati per sabato e domenica per 4 persone",
        "attrezzatura": "Ho ordinato i miei nuovi sci su Amazon con consegna garantita venerdì mattina prima della partenza; gli altri hanno già la propria attrezzatura",
        "trasporto": "Partenza in auto privata dell'utente venerdì pomeriggio dopo l'arrivo del pacco",
        "condizioni_neve": "Attualmente la neve sulle piste è insufficiente per sciare; le previsioni indicano una nevicata fondamentale giovedì notte che permetterebbe l'apertura degli impianti"
    },
    "profilo_e_vincoli_partecipanti": {
        "Gianni": "Lavora come macchinista/ferroviere per le ferrovie italiane (soggetto a turni e precettazioni straordinarie in caso di emergenze di trasporto)",
        "Erica": "Estremamente superstiziosa (crede ciecamente a presagi di sfortuna, numeri infausti, specchi rotti, venerdì 17)",
        "Diana": "Ha un figlio piccolo di 4 anni che lascerà a sua madre per il weekend; è affetta da grave allergia a pecore, lana grezza e ovini",
        "Io": "Ho una fidanzata di nazionalità russa che vive con me"
    }
}

QUESTIONS_INDIVIDUAL = {
    "is_relevant": {
        "type": "noul",
        "instructions": "Is the incoming event relevant directly or indirectly to the active plan, its logistics, locations, or participants?",
    },
    "impact_level": {
        "type": "choice",
        "instructions": "Rate the degree of impact this event has on the feasibility or smooth execution of the plan.",
        "criteria": {
            "NO": "Event is unrelated, harmless, or already mitigated (e.g. affects a different location or unimpacted detail).",
            "POCO": "Minor inconvenience or interesting notice, plan proceeds with minimal or no adjustments.",
            "MOLTO": "Significant disruption requiring active verification, contingency replanning, or team communication.",
            "CRITICAL": "Severe blocker, hazard, or obstacle that prevents the plan from proceeding as intended.",
        },
    },
}


def call_jev(state: dict[str, Any], questions: dict[str, Any], api_key: str) -> dict[str, Any]:
    payload = {
        "model": DEFAULT_MODEL,
        "state": state,
        "questions": questions,
    }
    t0 = time.perf_counter()
    req = urllib.request.Request(
        TYPESAFE_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Basalt-Jev-Tester/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = (time.perf_counter() - t0) * 1000
            return {"status": "ok", "elapsed_ms": elapsed, "data": data}
    except urllib.error.HTTPError as e:
        elapsed = (time.perf_counter() - t0) * 1000
        error_msg = e.read().decode("utf-8", errors="replace")
        return {"status": "error", "code": e.code, "message": error_msg, "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        return {"status": "error", "message": str(e), "elapsed_ms": elapsed}


def run_all_tests():
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("ERROR: TYPESAFE_API_KEY not set!")
        sys.exit(1)

    print("================================================================================")
    print("AVVIO SUITE COMPLETA TEST JEV SYSTEM ONE LIVE")
    print("================================================================================")

    results = []

    # 1. Categoria 1: Diretti Evidenti
    cat1_tests = [
        ("CAT1-DIR-1", "Gianni polmonite acuta", "Gianni ha avuto un malore improvviso e gli è stata diagnosticata una polmonite acuta: il medico gli ha prescritto un mese intero di riposo a letto."),
        ("CAT1-DIR-2", "Cancellazione Hotel Cervinia", "L'Hotel di Cervinia invia una comunicazione urgente: per un grave guasto all'impianto idraulico la struttura rimarrà chiusa per tutto il weekend e le prenotazioni sono annullate."),
        ("CAT1-DIR-3", "Strada Regionale Chiusa", "Protezione Civile: a causa di un cedimento della carreggiata, l'unica strada regionale che sale a Cervinia è chiusa al traffico fino a martedì prossimo."),
    ]

    # 2. Categoria 2: Chiaramente Non Correlati
    cat2_tests = [
        ("CAT2-NEG-1", "Farfalla Amazzonia", "Entomologi hanno classificato una nuova specie di farfalla variopinta nella foresta pluviale amazzonica."),
        ("CAT2-NEG-2", "Esopianeta James Webb", "Il telescopio spaziale James Webb ha confermato la presenza di vapore acqueo nell'atmosfera di un esopianeta a 120 anni luce."),
        ("CAT2-NEG-3", "Croissant al pistacchio", "La pasticceria all'angolo della via di casa ha introdotto nel menù colazioni i nuovi croissant ripieni al pistacchio di Bronte."),
    ]

    # 3. Categoria 3: Indiretti Sottili (Brevi e Lunghi)
    cat3_tests = [
        ("CAT3-IND-1-BREVE", "Sciopero Corrieri (Breve)", "Da giovedì a sabato proclamato sciopero nazionale di tutti i corrieri espresso e degli addestratori cinofili."),
        ("CAT3-IND-1-LUNGA", "Sciopero Corrieri (Lunga)", "Notiziario sindacale: mentre gli addestratori cinofili protestano per le concessioni dei parchi urbani, i sindacati della logistica hanno indetto un blocco generale di 48 ore delle consegne di Amazon, DHL e GLS tra giovedì e sabato pomeriggio."),
        ("CAT3-IND-2-BREVE", "Madre di Diana in partenza (Breve)", "La madre di Diana è dovuta partire d'urgenza per la Sicilia perché sua madre anziana ha avuto un grave problema di salute."),
        ("CAT3-IND-2-LUNGA", "Madre di Diana in partenza (Lunga)", "Messaggio ricevuto in famiglia: 'Cara Diana, mi dispiace immensamente ma la zia mi ha chiamato: la nonna è stata ricoverata d'urgenza a Palermo. Ho dovuto fare la valigia e prendere il primo aereo; non sarò a casa per tutto il fine settimana'."),
        ("CAT3-IND-3-BREVE", "Sagra dell'Ovino a Cervinia (Breve)", "Nel weekend a Cervinia si terrà la fiera dell'artigianato e la storica fiera dell'ovino con pascolo aperto nelle vie del centro."),
        ("CAT3-IND-3-LUNGA", "Sagra dell'Ovino a Cervinia (Lunga)", "Turismo Valle d'Aosta: grande attesa questo fine settimana nel borgo di Cervinia per la tradizionale transumanza e festa dell'ovino: oltre 600 pecore sfileranno tra i negozi accompagnate da stand gastronomici e lavorazione dal vivo della lana grezza."),
        ("CAT3-IND-4-BREVE", "Guerra Russia-Austria (Breve)", "Crisi bellica tra Russia e Austria: stazioni piene di profughi, il Ministero emette precettazione obbligatoria per tutti i ferrovieri con turni h24."),
        ("CAT3-IND-4-LUNGA", "Guerra Russia-Austria (Lunga)", "Aggiornamento geopolitico: dopo l'improvviso scontro al confine tra Russia e Austria, migliaia di sfollati si stanno riversando verso l'Italia. Il governo ha decretato lo stato di mobilitazione e la precettazione straordinaria del personale ferroviario per garantire i treni speciali, mentre i cittadini russi residenti affrontano forti restrizioni."),
        ("CAT3-IND-5-BREVE", "Superstizione Erica 13 Specchi (Breve)", "Erica è in lacrime e sotto shock: suo figlio giocando a pallone in casa ha fatto cadere una credenza rompendo 13 specchi in un colpo solo."),
        ("CAT3-IND-5-LUNGA", "Superstizione Erica 13 Specchi (Lunga)", "Erica scrive disperata sul gruppo: 'Ragazzi, non so cosa fare... mio figlio tirando un pallone ha travolto la vetrina con i 13 specchi antichi, sono tutti in frantumi. Per me è un presagio orribile, con 13 specchi rotti ho il terrore persino di mettere piede fuori dalla porta questo weekend'."),
        ("CAT3-IND-6-BREVE", "Hacking Radar Meteo (Breve)", "Il collettivo hacker Umbrella Corporation ha manomesso i radar e i database dei modelli previsionali meteorologici italiani."),
        ("CAT3-IND-6-LUNGA", "Hacking Radar Meteo (Lunga)", "Comunicato sicurezza informatica: un attacco hacker della Umbrella Corporation ha compromesso i server dell'Aeronautica e dei principali servizi meteo, iniettando perturbazioni fittizie e rendendo prive di attendibilità tutte le previsioni di nevicate per il nord Italia."),
    ]

    # 4. Categoria 4: Falsi Positivi Ingannevoli
    cat4_tests = [
        ("CAT4-FALSE-1", "Impianti chiusi a Cortina", "A causa di un cedimento strutturale della fune portante, tutti gli impianti da sci della conca di Cortina d'Ampezzo resteranno chiusi per il weekend."),
        ("CAT4-FALSE-2", "Prezzi degli sci +300%", "Mercato sportivo: da oggi tutti i produttori di sci alpino hanno aumentato il prezzo di listino del 300% a causa della carenza globale di materiali."),
        ("CAT4-FALSE-3", "Sciopero ferrovie Germania", "Germania: i macchinisti della Deutsche Bahn hanno indetto uno sciopero totale di 48 ore bloccando tutto il traffico ferroviario tedesco."),
        ("CAT4-FALSE-4", "Sagra del Bovino Cervinia", "Cervinia: questo sabato si terrà la tradizionale sagra del bovino di razza valdostana e della fontina con degustazioni in piazza."),
        ("CAT4-FALSE-5", "Specchi rotti a Milano", "Milano: incidente durante il trasloco di una vetreria, un furgone urta il marciapiede distruggendo decine di grandi specchi in esposizione."),
    ]

    all_individual = [("CAT 1 (Diretti)", cat1_tests),
                      ("CAT 2 (Negativi)", cat2_tests),
                      ("CAT 3 (Indiretti)", cat3_tests),
                      ("CAT 4 (Falsi Positivi)", cat4_tests)]

    test_logs = []

    for cat_name, test_list in all_individual:
        print(f"\n--- {cat_name} ---")
        for tid, label, event_text in test_list:
            state = {
                "piano_attivo": PLAN_CARD,
                "evento_in_arrivo": event_text,
            }
            res = call_jev(state, QUESTIONS_INDIVIDUAL, api_key)
            if res["status"] == "ok":
                answers = res["data"]["answers"]
                rel = answers["is_relevant"]["noul"]
                impact = answers["impact_level"]["choice"]
                conf = answers["impact_level"]["confidence"]
                ms = res["elapsed_ms"]
                tokens = res["data"].get("usage", {})
                print(f"[{tid}] {label} -> Rel: {rel:.2f} | Impact: {impact} (Conf: {conf:.2f}) | {ms:.0f}ms")
                test_logs.append({
                    "id": tid,
                    "category": cat_name,
                    "label": label,
                    "event": event_text,
                    "relevance": rel,
                    "impact": impact,
                    "confidence": conf,
                    "probabilities": answers["impact_level"].get("probabilities", {}),
                    "elapsed_ms": ms,
                    "tokens": tokens,
                })
            else:
                print(f"[{tid}] {label} -> ERRORE: {res.get('message')}")
            time.sleep(0.1)

    # 5. TEST DI AGGLOMERAZIONE BATCH (10 EVENTI A PACCHETTO)
    print("\n================================================================================")
    print("TEST AGGLOMERAZIONE BATCH (10 EVENTI IN UN SOLO PASSAGGIO)")
    print("================================================================================")

    # Batch A: 9 eventi innocui + 1 evento critico (Sciopero corrieri Amazon)
    batch_a_events = [
        "1. Nuova specie di farfalla scoperta nella foresta amazzonica.",
        "2. Pasticceria di quartiere lancia croissant al pistacchio.",
        "3. Telescopio James Webb rileva vapore acqueo su esopianeta.",
        "4. Uscito l'ultimo trailer del film d'animazione Disney per Natale.",
        "5. Sciopero nazionale proclamato per tutti i corrieri espresso Amazon, DHL e GLS tra giovedì e sabato.",  # DISCORDE CRITICO
        "6. Campionato di scacchi under 18 ha registrato record di iscrizioni.",
        "7. Asta di auto d'epoca a Londra batte record mondiale per una Ferrari del 1962.",
        "8. Iniziati i lavori per la nuova pista ciclabile nel comune di Firenze.",
        "9. Piantati 500 nuovi alberi nel parco cittadino per la festa dell'albero.",
        "10. Ristorante storico di Napoli vince premio per la migliore margherita tradizionale."
    ]

    # Batch B: 9 eventi critici + 1 evento innocuo (Croissant pistacchio)
    batch_b_events = [
        "1. Frana chiude completamente l'unica strada di accesso a Cervinia.",
        "2. Hotel di Cervinia annulla tutte le prenotazioni del weekend per rottura tubature.",
        "3. Gianni è ricoverato per polmonite acuta e deve rimanere a letto un mese.",
        "4. Sciopero corrieri Amazon giovedì e venerdì: pacchi attrezzatura bloccati.",
        "5. La madre di Diana è partita d'urgenza e Diana non ha nessuno a cui lasciare il figlio.",
        "6. Cervinia ospita la transumanza con 600 pecore in centro (Diana allergica).",
        "7. Pasticceria sotto casa presenta croissant al pistacchio di Bronte.",  # DISCORDE INNOCUO
        "8. Sciopero generale e precettazione straordinaria di tutti i ferrovieri per emergenza profughi.",
        "9. Hacker manomettono i radar meteo italiani: impossibile sapere se nevicherà.",
        "10. Erica in lacrime dopo che 13 specchi si sono rotti in casa sua."
    ]

    batch_questions = {
        "has_blocker_or_critical": {
            "type": "noul",
            "instructions": "Does this list of 10 incoming events contain AT LEAST ONE event that critically disrupts, blocks, or threatens the active ski trip plan?",
        },
        "critical_event_detection": {
            "type": "choice",
            "instructions": "Evaluate the presence and nature of critical events affecting the plan in this batch.",
            "criteria": {
                "NO_CRITICAL_EVENT": "None of the events threaten or affect the ski trip plan (all 10 are harmless/unrelated).",
                "SINGLE_CRITICAL_EVENT_FOUND": "Exactly one or isolated critical event found that threatens the plan among mostly harmless news.",
                "PREVALENT_OR_MULTIPLE_CRITICAL_EVENTS": "Multiple or almost all events disrupt the plan with severe obstacles.",
            },
        },
    }

    # Esecuzione Batch A
    print("\n--- ESECUZIONE BATCH A (9 Innocui + 1 Critico) ---")
    state_a = {
        "piano_attivo": PLAN_CARD,
        "digest_10_eventi": "\n".join(batch_a_events),
    }
    res_batch_a = call_jev(state_a, batch_questions, api_key)
    if res_batch_a["status"] == "ok":
        ans_a = res_batch_a["data"]["answers"]
        has_blocker = ans_a["has_blocker_or_critical"]["noul"]
        crit_choice = ans_a["critical_event_detection"]["choice"]
        conf_a = ans_a["critical_event_detection"]["confidence"]
        ms_a = res_batch_a["elapsed_ms"]
        print(f"Batch A -> Has Blocker: {has_blocker:.2f} | Detection: {crit_choice} (Conf: {conf_a:.2f}) | {ms_a:.0f}ms")
    else:
        print(f"Batch A -> ERRORE: {res_batch_a.get('message')}")

    # Esecuzione Batch B
    print("\n--- ESECUZIONE BATCH B (9 Critici + 1 Innocuo) ---")
    state_b = {
        "piano_attivo": PLAN_CARD,
        "digest_10_eventi": "\n".join(batch_b_events),
    }
    res_batch_b = call_jev(state_b, batch_questions, api_key)
    if res_batch_b["status"] == "ok":
        ans_b = res_batch_b["data"]["answers"]
        has_blocker_b = ans_b["has_blocker_or_critical"]["noul"]
        crit_choice_b = ans_b["critical_event_detection"]["choice"]
        conf_b = ans_b["critical_event_detection"]["confidence"]
        ms_b = res_batch_b["elapsed_ms"]
        print(f"Batch B -> Has Blocker: {has_blocker_b:.2f} | Detection: {crit_choice_b} (Conf: {conf_b:.2f}) | {ms_b:.0f}ms")
    else:
        print(f"Batch B -> ERRORE: {res_batch_b.get('message')}")

    # Salva il dump dei risultati per l'analisi
    dump_data = {
        "individual_tests": test_logs,
        "batch_a": res_batch_a,
        "batch_b": res_batch_b,
    }
    with open("E:/projects/basalt/tools/jev_test_results.json", "w", encoding="utf-8") as f:
        json.dump(dump_data, f, indent=2, ensure_ascii=False)
    print("\nRisultati completi esportati in tools/jev_test_results.json")


if __name__ == "__main__":
    run_all_tests()
