"""Proof of Concept (PoC) for Basalt Omniverse: TypeSafe Jev System One + Qwen.

This PoC demonstrates the dual-process architecture:
1. System 1 (Jev / TypeSafe): Fast, typed, calibrated triage of open-ended incoming events
   against cached plan insight cards.
2. System 2 (Qwen / Basalt): Deep reasoning, causal analysis, and autonomous execution
   triggered only when Jev flags an impact as 'MOLTO' or 'CRITICAL'.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


def evaluate_with_jev(state: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    """Call TypeSafe Jev System One API or fallback to calibrated simulation if no key is set."""
    key = api_key or os.environ.get("TYPESAFE_API_KEY")
    
    questions = {
        "is_relevant": {
            "type": "noul",
            "instructions": "Is the incoming event relevant to the active plan, its location, participants, or execution?",
        },
        "impact_level": {
            "type": "choice",
            "instructions": "Rate the degree of impact this event has on the feasibility or execution of the plan.",
            "criteria": {
                "NO": "Unrelated or completely harmless event.",
                "POCO": "Minor inconvenience, can proceed with minimal or no adjustments.",
                "MOLTO": "Significant disruption requiring active verification or adjustments.",
                "CRITICAL": "Severe blocker or hazard that prevents the plan from proceeding as intended.",
            },
        },
    }

    payload = {
        "model": DEFAULT_MODEL,
        "state": state,
        "questions": questions,
    }

    if not key:
        return _mock_jev_response(state)

    req = urllib.request.Request(
        TYPESAFE_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Basalt-Omniverse-PoC/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"status": "live", "data": data}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"status": "error", "code": e.code, "message": error_body}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _mock_jev_response(state: dict[str, Any]) -> dict[str, Any]:
    """Deterministic simulation reflecting Jev's calibrated scoring for demonstration."""
    event = state.get("incoming_event", "").lower()
    
    if "valanga" in event or "cervinia distrutta" in event:
        score = "CRITICAL"
        prob_rel = 0.99
        conf = 0.98
    elif "multa" in event or "febbre" in event or "non posso" in event or "800" in event:
        score = "MOLTO"
        prob_rel = 0.96
        conf = 0.92
    elif "meteo" in event or "neve" in event or "pioggia" in event:
        score = "POCO"
        prob_rel = 0.82
        conf = 0.85
    else:
        score = "NO"
        prob_rel = 0.04
        conf = 0.99

    return {
        "status": "simulated (TYPESAFE_API_KEY not set in env)",
        "data": {
            "answers": {
                "is_relevant": {"type": "noul", "noul": prob_rel},
                "impact_level": {
                    "type": "choice",
                    "choice": score,
                    "confidence": conf,
                    "probabilities": {
                        "NO": 0.01 if score != "NO" else 0.98,
                        "POCO": 0.05 if score != "POCO" else 0.85,
                        "MOLTO": 0.92 if score == "MOLTO" else 0.03,
                        "CRITICAL": 0.95 if score == "CRITICAL" else 0.01,
                    },
                },
            }
        },
    }


def handoff_to_qwen_if_needed(plan: dict, event: str, jev_result: dict) -> None:
    """Evaluate Jev's output and determine if Qwen on Blackwell must be awakened."""
    answers = jev_result.get("data", {}).get("answers", {})
    relevance = answers.get("is_relevant", {}).get("noul", 0.0)
    impact_data = answers.get("impact_level", {})
    impact = impact_data.get("choice", "NO")
    confidence = impact_data.get("confidence", 0.0)

    print(f"  [Jev Evaluation] Relevance: {relevance:.2f} | Impact: {impact} (Confidence: {confidence:.2f})")

    if impact in ("MOLTO", "CRITICAL") and relevance > 0.5:
        print("  --> [ACTION] SOGLIA SUPERATA! Risveglio di Qwen (System 2 su Blackwell sm_120)...")
        print(f"      Qwen Causal Prompt: 'L'evento \"{event}\" ha impatto {impact} sul piano \"{plan['title']}\".'")
        if impact == "CRITICAL":
            print("      [Qwen Action]: Iniziativa ad alta priorità -> Chiama hotel con callAPIcall / Notifica push immediata.")
        else:
            print("      [Qwen Action]: Iniziativa in background -> Calcola piano di contingenza / Prepara alternative.")
    elif impact == "POCO":
        print("  --> [ACTION] Impatto marginale: Registrazione silenziosa nel log MongoDB del piano (nessun disturbo all'utente).")
    else:
        print("  --> [ACTION] Evento irrilevante: Scartato a costo zero (0 token Qwen sprecati).")


def run_poc():
    print("=" * 80)
    print("BASALT OMNIVERSE: PoC JEV SYSTEM 1 + QWEN SYSTEM 2")
    print("=" * 80)

    plan_card = {
        "title": "Weekend a Cervinia (24-26 Ottobre)",
        "goal": "Sci e relax in montagna. Hotel Grandes Murailles prenotato per 3 persone.",
        "participants": ["Utente", "Gianna", "Lucia"],
        "transport": "Auto dell'utente",
        "destination": "Cervinia, Valle d'Aosta",
    }

    test_events = [
        "Hey, hai sentito che Cervinia è stata distrutta da una valanga?",
        "Lucia scrive: 'Ragazzi mi hanno fatto 800€ di multa, sono al verde e non posso più venire'",
        "Gianna manda un vocale: 'Ho 39 di febbre e la tosse, non riesco ad alzarmi dal letto'",
        "Supermercato sotto casa: sconti del 30% su pasta e surgelati per tutto il weekend",
        "Previsioni meteo Cervinia: prevista spolverata di neve di 3 cm nella notte di venerdì",
    ]

    print("\n[PIANO ATTIVO IN MEMORIA]:")
    print(json.dumps(plan_card, indent=2, ensure_ascii=False))
    print("\n" + "-" * 80)

    for i, event in enumerate(test_events, 1):
        print(f"\n[Test Case {i}] Nuovo Evento Rilevato:")
        print(f"  \"{event}\"")
        state = {
            "active_plan": plan_card,
            "incoming_event": event,
        }
        result = evaluate_with_jev(state)
        handoff_to_qwen_if_needed(plan_card, event, result)
        print("-" * 80)


if __name__ == "__main__":
    run_poc()
