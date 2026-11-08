"""Bridge testuale fra il Telegram Wrapper e il cervello (brains.py).

Non e' un servizio che parte da solo con CallAPICall: si avvia a mano quando
serve, con:

    python -m callbridge.telegram_text_bridge

Flusso:
  Telegram Wrapper --[webhook POST, messaggio nuovo]--> questo bridge
      --[testo taggato "e' Telegram"]--> brain.ask_stream() (cervello di brains.py)
      --[risposta testuale, se il cervello decide di rispondere]--> REST del
      Telegram Wrapper (`POST /message/send`) sullo stesso chat_id.

Il bridge non decide COSA rispondere né SE rispondere: quella logica (profili,
tempi, comportamento nei gruppi) vive nello skill `telegram-wrapper` letto dal
cervello. Il bridge si limita a inoltrare nei due sensi, e a riconoscere la
convenzione con cui il cervello dice "non rispondo a questo" (vedi
`_NESSUNA_RISPOSTA`): senza questa convenzione, un messaggio di gruppo a cui
cervello sceglie di non reagire finirebbe comunque rimandato come messaggio
vuoto o come errore.
"""
from __future__ import annotations

import logging
import threading

import requests
from flask import Flask, jsonify, request

from .brains import make_brain
from .config import Config

log = logging.getLogger("callbridge.telegram_bridge")

#: Stringa esatta con cui il cervello segnala "ho deciso di non rispondere a
#: questo messaggio" (tipicamente in un gruppo dove non e' il caso di
#: intervenire). Va comunicata al cervello nel preambolo di ogni messaggio:
#: senza una convenzione esplicita, il bridge non ha modo di distinguere
#: "niente da dire" da "risposta vuota per errore".
NESSUNA_RISPOSTA = "[NESSUNA RISPOSTA]"


def _preambolo(payload: dict) -> str:
    """Compone il testo taggato che arriva al cervello: dice chiaramente che e'
    un messaggio Telegram, chi lo manda, da dove, e come astenersi."""
    tipo_chat = "gruppo" if payload.get("is_group") else "privata"
    username = payload.get("from_username") or "—"
    return (
        "[Messaggio Telegram ricevuto]\n"
        f"Da: {payload.get('from_first_name', '?')} "
        f"(@{username}, id={payload.get('from_user_id')})\n"
        f"Chat: {tipo_chat} (chat_id={payload.get('chat_id')})\n"
        f"Testo: {payload.get('text', '')}\n\n"
        f"Se decidi di non rispondere a questo messaggio (per esempio perche' "
        f"in un gruppo non e' il caso di intervenire), rispondi esattamente "
        f"con: {NESSUNA_RISPOSTA} e nient'altro."
    )


class TelegramTextBridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Un solo cervello condiviso per tutti i messaggi, come fa il resto
        # del bridge: la conversazione dell'agente e' persistente.
        self.brain = make_brain(cfg)
        self.app = Flask("telegram_text_bridge")
        self.app.add_url_rule("/telegram/message", methods=["POST"],
                              view_func=self._on_message)

    def _on_message(self):
        payload = request.get_json(silent=True) or {}
        if payload.get("event") != "new_message":
            return jsonify({"status": "ignored"}), 200
        # Risponde subito al wrapper e processa in background: il cervello puo'
        # metterci diversi secondi, e non c'e' ragione di far aspettare la
        # POST del wrapper (che non fa nulla con la risposta di questa
        # richiesta, legge solo lo status code per il suo log).
        threading.Thread(target=self._handle, args=(payload,), daemon=True).start()
        return jsonify({"status": "accepted"}), 202

    def _handle(self, payload: dict) -> None:
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        is_group = bool(payload.get("is_group"))
        try:
            testo = "".join(
                chunk for chunk in self.brain.ask_stream(_preambolo(payload))
                if isinstance(chunk, str)
            ).strip()
        except Exception as e:  # noqa: BLE001 - il cervello puo' fallire per mille motivi
            log.error("Bridge Telegram: il cervello ha fallito su chat %s: %s", chat_id, e)
            return

        if not testo or testo == NESSUNA_RISPOSTA:
            log.info("Bridge Telegram: nessuna risposta per chat %s (scelta dell'agente)", chat_id)
            return

        self._invia_a_wrapper(chat_id, testo, message_id, is_group)

    def _invia_a_wrapper(self, chat_id, testo: str, reply_to: int | None, is_group: bool) -> None:
        url = f"{self.cfg.telegram_wrapper_url}/message/send"
        headers = {"Authorization": f"Bearer {self.cfg.telegram_wrapper_token}"}
        body = {
            "chat_id": chat_id,
            "content": {"text": testo},
            "reply_to_message_id": reply_to,
            "is_group": is_group,
        }
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=15)
            if resp.status_code == 200:
                log.info("Bridge Telegram: risposta inviata a chat %s", chat_id)
            else:
                log.warning("Bridge Telegram: invio fallito (%s): %s",
                           resp.status_code, resp.text[:200])
        except Exception as e:  # noqa: BLE001 - rete/wrapper non raggiungibile
            log.error("Bridge Telegram: impossibile contattare il wrapper: %s", e)

    def run(self) -> None:
        log.info("Bridge testuale Telegram in ascolto su http://%s:%d/telegram/message",
                 self.cfg.telegram_bridge_host, self.cfg.telegram_bridge_port)
        log.info("Risposte dirette al wrapper su %s", self.cfg.telegram_wrapper_url)
        self.app.run(host=self.cfg.telegram_bridge_host,
                    port=self.cfg.telegram_bridge_port)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.load()
    if not cfg.telegram_wrapper_token:
        log.warning("telegram_wrapper_token non impostato in config.json: le "
                   "risposte verso il wrapper verranno rifiutate (401)")
    TelegramTextBridge(cfg).run()


if __name__ == "__main__":
    main()
