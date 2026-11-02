"""The exact texts of the three level-4 dialogs, so what the user saw is what the dossier records.

These are working texts for the owner and a lawyer to review before the application is distributed.
"""
from __future__ import annotations

from typing import Dict

DIALOG_TEXTS: Dict[str, Dict[str, str]] = {
    "it": {
        "waiver": (
            "Stai per togliere la tua conferma ai pagamenti automatici di questo sistema di pagamento, fino a un massimo che "
            "scegli tu. Sotto quel massimo Basalt non filtra importi né beneficiari. Un sito o un'email manipolati possono "
            "far spendere all'agente fino a quel massimo senza che nessuno lo fermi. Scegli una cifra che sei disposto a "
            "perdere. Per continuare scrivi, o incolla, la dichiarazione qui sotto."),
        "release": (
            "Dichiaro di sapere che gli autori e i distributori di Basalt non rispondono delle conseguenze dei pagamenti "
            "eseguiti in autonomia dall'applicazione, nei limiti consentiti dalla legge."),
        "acceptance": (
            "Accetto integralmente la responsabilità dell'uso del livello 4 per questo sistema di pagamento. La rinuncia è "
            "personale, legata a questo utente del sistema operativo, e non è trasferibile."),
    },
    "en": {
        "waiver": (
            "You are about to remove your confirmation of automatic payments for this payment system, up to a maximum you "
            "choose. Below that maximum Basalt does not filter amounts or recipients. A manipulated site or email can make "
            "the agent spend up to that maximum with nobody stopping it. Choose an amount you are willing to lose. To "
            "continue, type or paste the declaration below."),
        "release": (
            "I understand that the authors and distributors of Basalt are not liable for the consequences of payments the "
            "application makes on its own, to the extent the law allows."),
        "acceptance": (
            "I fully accept responsibility for using level 4 with this payment system. The waiver is personal, tied to this "
            "operating-system user, and cannot be transferred."),
    },
}
