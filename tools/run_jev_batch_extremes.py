"""Test runner for extreme batch tests:
Batch C: 10 out of 10 events are critical/disruptive for the ski trip plan.
Batch D: 0 out of 10 events are impactful (all 10 are completely unrelated/harmless).
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

# 10 eventi TUTTI critici / influenti sul piano
BATCH_C_ALL_IMPACTFUL = [
    "1. Protezione Civile: cedimento strutturale della carreggiata, l'unica strada regionale per Cervinia è chiusa a tutti i veicoli fino a martedì prossimo.",
    "2. L'Hotel prenotato a Cervinia comunica la cancellazione di tutte le prenotazioni del weekend per rottura totale dell'impianto di riscaldamento.",
    "3. Gianni è stato ricoverato d'urgenza con polmonite acuta e deve osservare un mese di riposo assoluto a letto.",
    "4. Sciopero nazionale a oltranza di tutti i corrieri espresso Amazon, DHL e GLS: consegne di pacchi e attrezzature sportive bloccate fino a lunedì.",
    "5. La madre di Diana è dovuta partire d'urgenza per la Sicilia per assistere la nonna gravemente malata; Diana non ha nessuno a cui affidare il figlio di 4 anni.",
    "6. Il comune di Cervinia ha inaugurato la storica fiera dell'ovino: oltre 600 pecore sfilano libere per le vie del centro con banchi di lana grezza (Diana è gravemente allergica agli ovini).",
    "7. Crisi dei trasporti e stato di mobilitazione: il Ministero dei Trasporti emette ordine di precettazione obbligatoria h24 per tutti i macchinisti delle ferrovie italiane, incluso Gianni.",
    "8. Aggiornamento meteorologico definitivo: l'anticiclone africano blocca la perturbazione; zero neve prevista per giovedì e impianti di Cervinia chiusi per totale assenza di manto nevoso.",
    "9. Guasto catastrofico alla linea elettrica di alta quota di Cervinia: skipass annullati e rimborsati d'ufficio, tutti gli impianti di risalita rimarranno fermi nel weekend.",
    "10. Provvedimento consolare urgente: restrizioni e obblighi burocratici per i residenti di nazionalità russa in Italia con convocazione obbligatoria nel weekend per la fidanzata dell'utente."
]

# 10 eventi TUTTI innocui / non correlati
BATCH_D_NONE_IMPACTFUL = [
    "1. Entomologi hanno classificato una nuova specie di farfalla variopinta nella foresta pluviale amazzonica.",
    "2. Il telescopio spaziale James Webb ha confermato la presenza di vapore acqueo nell'atmosfera di un esopianeta a 120 anni luce.",
    "3. La pasticceria all'angolo della via di casa ha introdotto nel menù colazioni i nuovi croissant ripieni al pistacchio di Bronte.",
    "4. La Disney ha rilasciato nei cinema il nuovo trailer promozionale del film d'animazione per le feste di Natale.",
    "5. Il torneo giovanile di scacchi under 18 ha registrato un nuovo record storico di iscritti da tutta Italia.",
    "6. Asta d'auto d'epoca a Londra: una rara Ferrari 250 GTO del 1962 è stata battuta all'asta a una cifra record.",
    "7. Inaugurata la nuova pista ciclabile alberata lungo il fiume Arno nella periferia est di Firenze.",
    "8. Piantati oltre 500 nuovi alberi di quercia nel parco naturale collinare in occasione della giornata nazionale dell'albero.",
    "9. Storica pizzeria sul lungomare di Napoli riceve il premio internazionale per la migliore margherita tradizionale.",
    "10. Ricercatori dell'università di Kyoto hanno sviluppato un innovativo enzima biologico per accelerare il riciclo della plastica PET."
]

BATCH_QUESTIONS = {
    "has_blocker_or_critical": {
        "type": "noul",
        "instructions": "Does this list of 10 incoming events contain AT LEAST ONE event that critically disrupts, blocks, or threatens the active ski trip plan?",
    },
    "batch_impact_scope": {
        "type": "choice",
        "instructions": "Evaluate the overall proportion and nature of events in this 10-event batch affecting the ski trip plan.",
        "criteria": {
            "ALL_EVENTS_IMPACTFUL": "All or virtually all 10 events directly or indirectly disrupt, block, or severely impact the ski trip plan.",
            "MIXED_SOME_IMPACTFUL": "A mixture containing some impactful events alongside unrelated or harmless news (between 1 and 7 events affect the plan).",
            "ZERO_EVENTS_IMPACTFUL": "None of the 10 events affect the plan (all 10 are completely harmless, unrelated news or neutral happenings).",
        },
    },
    "action_required": {
        "type": "choice",
        "instructions": "What level of system intervention is warranted by this batch of 10 events?",
        "criteria": {
            "NO_ACTION_REQUIRED": "No disruptive events detected. The plan proceeds as scheduled without any adjustments.",
            "LOCAL_ADJUSTMENT_OR_SINGLE_ALERT": "Only isolated adjustments or a single alert required.",
            "SYSTEMIC_OVERHAUL_OR_CANCELLATION": "Pervasive catastrophe across logistics, participants, or venue; requires immediate major replanning or cancellation.",
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


def main():
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("ERROR: TYPESAFE_API_KEY environment variable not set!")
        sys.exit(1)

    print("================================================================================")
    print("ESECUZIONE TEST ESTREMI BATCH JEV (10/10 INFLUENTI vs 0/10 INFLUENTI)")
    print("================================================================================")

    # 1. BATCH C: 10 TUTTI INFLUENTI
    print("\n--- ESECUZIONE BATCH C (10 su 10 TUTTI INFLUENTI / CRITICI) ---")
    state_c = {
        "piano_attivo": PLAN_CARD,
        "digest_10_eventi": "\n".join(BATCH_C_ALL_IMPACTFUL),
    }
    res_c = call_jev(state_c, BATCH_QUESTIONS, api_key)
    if res_c["status"] == "ok":
        ans_c = res_c["data"]["answers"]
        has_blocker_c = ans_c["has_blocker_or_critical"]["noul"]
        scope_c = ans_c["batch_impact_scope"]["choice"]
        scope_conf_c = ans_c["batch_impact_scope"]["confidence"]
        scope_probs_c = ans_c["batch_impact_scope"].get("probabilities", {})
        action_c = ans_c["action_required"]["choice"]
        action_conf_c = ans_c["action_required"]["confidence"]
        action_probs_c = ans_c["action_required"].get("probabilities", {})
        ms_c = res_c["elapsed_ms"]
        tokens_c = res_c["data"].get("usage", {})

        print(f"Batch C -> Has Blocker (Noul): {has_blocker_c:.2f}")
        print(f"Batch C -> Scope (Choice): {scope_c} (Conf: {scope_conf_c:.2f}) | Probs: {scope_probs_c}")
        print(f"Batch C -> Action (Choice): {action_c} (Conf: {action_conf_c:.2f}) | Probs: {action_probs_c}")
        print(f"Batch C -> Tempo: {ms_c:.0f}ms | Tokens: {tokens_c}")
    else:
        print(f"Batch C -> ERRORE: {res_c.get('message')}")

    time.sleep(0.2)

    # 2. BATCH D: 0 TUTTI INNOCUI
    print("\n--- ESECUZIONE BATCH D (0 su 10 INFLUENTI / TUTTI INNOCUI) ---")
    state_d = {
        "piano_attivo": PLAN_CARD,
        "digest_10_eventi": "\n".join(BATCH_D_NONE_IMPACTFUL),
    }
    res_d = call_jev(state_d, BATCH_QUESTIONS, api_key)
    if res_d["status"] == "ok":
        ans_d = res_d["data"]["answers"]
        has_blocker_d = ans_d["has_blocker_or_critical"]["noul"]
        scope_d = ans_d["batch_impact_scope"]["choice"]
        scope_conf_d = ans_d["batch_impact_scope"]["confidence"]
        scope_probs_d = ans_d["batch_impact_scope"].get("probabilities", {})
        action_d = ans_d["action_required"]["choice"]
        action_conf_d = ans_d["action_required"]["confidence"]
        action_probs_d = ans_d["action_required"].get("probabilities", {})
        ms_d = res_d["elapsed_ms"]
        tokens_d = res_d["data"].get("usage", {})

        print(f"Batch D -> Has Blocker (Noul): {has_blocker_d:.2f}")
        print(f"Batch D -> Scope (Choice): {scope_d} (Conf: {scope_conf_d:.2f}) | Probs: {scope_probs_d}")
        print(f"Batch D -> Action (Choice): {action_d} (Conf: {action_conf_d:.2f}) | Probs: {action_probs_d}")
        print(f"Batch D -> Tempo: {ms_d:.0f}ms | Tokens: {tokens_d}")
    else:
        print(f"Batch D -> ERRORE: {res_d.get('message')}")

    # Salva i risultati
    dump = {
        "batch_c_all_impactful": res_c,
        "batch_d_none_impactful": res_d,
    }
    with open("E:/projects/basalt/tools/jev_batch_extremes_results.json", "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, ensure_ascii=False)
    print("\nRisultati esportati in tools/jev_batch_extremes_results.json")


if __name__ == "__main__":
    main()
