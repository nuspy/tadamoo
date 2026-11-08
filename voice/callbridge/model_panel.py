"""Pannello companion always-on-top per le sessioni avatar.

Piccola finestra Tk (usa il root dell'overlay, thread Tk principale) con:
  - dropdown del modello LLM di Hermes, raggruppato per provider ("provider / modello");
  - bottone "Nuova sessione" (azzera il contesto dell'agente);
  - trascrizione del turno: cio' che si e' detto e cio' che la LLM risponde,
    aggiornato mentre la risposta esce dal modello.

La trascrizione sta QUI e non disegnata sotto il video dell'avatar: in una
finestra vera il testo si legge, si seleziona e si copia, e non ruba pixel
all'immagine.

Mostrata quando parte una sessione avatar (Ctrl+Shift+F1/F2), nascosta a fine
sessione. Parla col control server del bridge (:8770) via HTTP.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("callbridge.model_panel")


class ModelPanel:
    """Pannello di controllo modello LLM, accanto alla finestra avatar."""

    def __init__(self, overlay, config) -> None:
        # `overlay` = BrainOverlay: possiede il root Tk e offre post()/get_root().
        self._overlay = overlay
        self._url = f"http://{config.control_host}:{config.control_port}"
        self._win = None
        self._combo = None
        self._testo = None
        self._flat: list[tuple[str, str, str]] = []  # (label, model, provider)
        # Lingua: (codici, etichette) e il valore mostrato adesso. `_lang_poll`
        # è l'id dell'after() Tk, per poterlo fermare quando il pannello si
        # nasconde (altrimenti continuerebbe a interrogare il bridge a vuoto).
        self._lang_combo = None
        self._lang_codes: list[str] = []
        self._lang_shown = ""
        self._lang_poll = None

    # --- API pubblica (thread-safe: instrada sul thread Tk) --------------

    def show(self) -> None:
        if self._overlay is not None:
            self._overlay.post(self._show_impl)

    def hide(self) -> None:
        if self._overlay is not None:
            self._overlay.post(self._hide_impl)

    def mostra_testo(self, ruolo: str, testo: str) -> None:
        """Scrive una riga della trascrizione ("utente" o "avatar").

        Si chiama molte volte per turno: la risposta della LLM arriva un pezzo
        alla volta e ogni chiamata porta il testo INTERO accumulato finora, non
        il solo pezzo nuovo. Quindi la riga si RISCRIVE, non si accoda —
        accodando si vedrebbe la stessa frase ripetuta decine di volte.
        """
        if self._overlay is not None:
            self._overlay.post(lambda: self._mostra_impl(ruolo, testo))

    def _mostra_impl(self, ruolo: str, testo: str) -> None:
        """Disegna il turno come una chat: tu a destra, l'avatar a sinistra.

        I "fumetti" sono tag Tk, non widget: un Text con `justify`, uno sfondo
        e dei margini asimmetrici da' la stessa lettura a colpo d'occhio di una
        chat vera, e resta un solo widget da aggiornare mentre la risposta
        scorre — con dei frame annidati bisognerebbe crearne e distruggerne uno
        per battuta.
        """
        t = self._testo
        if t is None:
            return                      # pannello non ancora aperto
        try:
            t.configure(state="normal")
            if ruolo == "utente":
                if t.index("end-1c") != "1.0":
                    t.insert("end", "\n")
                t.insert("end", (testo or "") + "\n", ("bolla_utente",))
                # Segnaposto per la risposta, che verra' riscritta in loco
                # mentre esce dal modello.
                t.mark_set("avatar_da", "end-1c")
                t.mark_gravity("avatar_da", "left")
            else:
                if "avatar_da" not in t.mark_names():
                    t.mark_set("avatar_da", "end-1c")
                    t.mark_gravity("avatar_da", "left")
                # Si cancella fino a "end-1c" e non a "end": Tk mantiene
                # sempre un a-capo suo, e cancellarlo attaccherebbe la
                # risposta alla riga precedente.
                t.delete("avatar_da", "end-1c")
                if testo:
                    t.insert("end-1c", testo, ("bolla_avatar",))
            t.see("end")
        except Exception as e:  # noqa: BLE001 - la trascrizione non deve mai
            log.debug("ModelPanel: trascrizione non aggiornata: %s", e)
        finally:
            try:
                t.configure(state="disabled")
            except Exception:  # noqa: BLE001
                pass

    # --- implementazione sul thread Tk -----------------------------------

    def _show_impl(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception as e:  # noqa: BLE001 - Tk assente
            log.debug("ModelPanel: Tk non disponibile: %s", e)
            return
        root = self._overlay.get_root()
        if root is None:
            return
        if self._win is None:
            self._win = tk.Toplevel(root)
            self._win.title("callAPIcall — Modello")
            self._win.attributes("-topmost", True)
            self._win.geometry("+40+40")
            ttk.Label(self._win, text="Modello LLM (Hermes)").pack(
                padx=8, pady=(8, 2), anchor="w")
            self._combo = ttk.Combobox(self._win, state="readonly", width=52)
            self._combo.pack(padx=8, pady=2)
            self._combo.bind("<<ComboboxSelected>>", self._on_select)
            # Lingua della conversazione: a DOPPIA VIA. Cambiandola qui la
            # sessione si adegua; se cambia per conto suo (comando vocale o
            # auto-rilevamento) il selettore la mostra entro un secondo.
            ttk.Label(self._win, text="Lingua").pack(
                padx=8, pady=(8, 2), anchor="w")
            self._lang_combo = ttk.Combobox(self._win, state="readonly", width=52)
            self._lang_combo.pack(padx=8, pady=2)
            self._lang_combo.bind("<<ComboboxSelected>>", self._on_lang_select)
            ttk.Button(self._win, text="Nuova sessione",
                       command=self._on_new).pack(padx=8, pady=(4, 8))
            ttk.Separator(self._win, orient="horizontal").pack(
                fill="x", padx=8, pady=(0, 6))
            cornice = ttk.Frame(self._win)
            cornice.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            barra = ttk.Scrollbar(cornice, orient="vertical")
            barra.pack(side="right", fill="y")
            # `state=disabled` in lettura: il testo lo scrive solo il
            # programma, ma resta selezionabile e copiabile.
            self._testo = tk.Text(cornice, width=54, height=10, wrap="word",
                                  yscrollcommand=barra.set, state="disabled",
                                  font=("Segoe UI", 9), relief="flat",
                                  background="#1b1b1b", foreground="#e0e0e0",
                                  insertbackground="#e0e0e0")
            self._testo.pack(side="left", fill="both", expand=True)
            barra.config(command=self._testo.yview)
            # Fumetti. Il margine dal lato OPPOSTO e' cio' che crea la
            # colonna: una bolla a destra lascia spazio vuoto a sinistra e
            # viceversa, cosi' si capisce chi parla senza leggere l'etichetta.
            # `spacing1/3` sono l'aria sopra e sotto, `borderwidth`+`relief`
            # danno il bordo del fumetto.
            self._testo.tag_configure(
                "bolla_utente", justify="right", background="#2f4f2f",
                foreground="#dff0df", lmargin1=70, lmargin2=70, rmargin=6,
                spacing1=5, spacing3=5, borderwidth=4, relief="flat",
                wrap="word")
            self._testo.tag_configure(
                "bolla_avatar", justify="left", background="#26384a",
                foreground="#dbeaf7", lmargin1=6, lmargin2=6, rmargin=70,
                spacing1=5, spacing3=5, borderwidth=4, relief="flat",
                wrap="word")
            # Chiudere la finestra la nasconde soltanto (la riapre la prossima sessione).
            self._win.protocol("WM_DELETE_WINDOW", self._hide_impl)
        self._win.deiconify()
        self._win.attributes("-topmost", True)
        threading.Thread(target=self._load_models, daemon=True).start()
        self._schedule_lang_poll()

    def _hide_impl(self) -> None:
        if self._lang_poll is not None and self._win is not None:
            try:
                self._win.after_cancel(self._lang_poll)
            except Exception:  # noqa: BLE001 - after già scaduto
                pass
            self._lang_poll = None
        if self._win is not None:
            try:
                self._win.withdraw()
            except Exception:  # noqa: BLE001
                pass

    def _fill(self, flat: list, cur: str | None) -> None:
        self._flat = flat
        if self._combo is None:
            return
        self._combo["values"] = [f[0] for f in flat]
        for i, (_lbl, m, _pr) in enumerate(flat):
            if m == cur:
                self._combo.current(i)
                break

    # --- rete (thread separati) ------------------------------------------

    def _load_models(self) -> None:
        try:
            import requests
            d = requests.get(self._url + "/hermes/models", timeout=20).json()
        except Exception as e:  # noqa: BLE001
            log.debug("ModelPanel: load modelli fallito: %s", e)
            return
        flat = []
        for p in d.get("providers", []):
            # `models_info` porta quantizzazione e peso: nel menu ci sono modelli
            # con lo STESSO nome e quantizzazioni diverse, e senza quei dati non
            # si capisce quale si sta scegliendo (ne' quanta VRAM chiede).
            info = {i["id"]: i for i in p.get("models_info") or []}
            for m in p.get("models", []):
                voce = info.get(m) or {}
                etichetta = voce.get("label") or m
                if voce.get("loaded"):
                    etichetta += "  ● in memoria"
                flat.append((f"{p['provider']} / {etichetta}", m, p["provider"]))
        cur = (d.get("current") or {}).get("model")
        self._overlay.post(lambda: self._fill(flat, cur))

    def _on_select(self, _evt=None) -> None:
        if self._combo is None:
            return
        i = self._combo.current()
        if 0 <= i < len(self._flat):
            _lbl, model, provider = self._flat[i]
            threading.Thread(target=self._post_model, args=(model, provider),
                             daemon=True).start()

    def _post_model(self, model: str, provider: str, confirm: bool = False) -> None:
        try:
            import requests
            r = requests.post(self._url + "/hermes/model", timeout=330,
                              json={"model": model, "provider": provider,
                                    "confirm": confirm})
            if r.status_code == 409:
                body = r.json() or {}
                if body.get("code") == "confirm_required":
                    # In VRAM ci sta un modello solo: lo scarico va CHIESTO.
                    # Il dialogo sta sul thread Tk; un "no" lascia tutto com'è.
                    self._overlay.post(lambda: self._ask_swap(model, provider, body))
                    return
                log.warning("ModelPanel: cambio modello rifiutato: %s", body.get("error"))
                return
            log.info("ModelPanel: modello -> %s (%s)", model, provider)
        except Exception as e:  # noqa: BLE001
            log.warning("ModelPanel: set modello fallito: %s", e)

    def _ask_swap(self, model: str, provider: str, info: dict) -> None:
        """Conferma dello swap: scarico del modello caldo + carico del nuovo."""
        try:
            from tkinter import messagebox
        except Exception:  # noqa: BLE001 - Tk assente
            return
        testo = (f"{info.get('reason', '')}\n\n"
                 f"Da scaricare:  {info.get('loaded_label') or info.get('loaded')}\n"
                 f"Da caricare:   {info.get('target_label') or model}\n\n"
                 "Procedo con lo scambio?")
        if not messagebox.askyesno("Cambio modello", testo, parent=self._win):
            log.info("ModelPanel: swap rifiutato, resta '%s'", info.get("loaded"))
            threading.Thread(target=self._load_models, daemon=True).start()  # ripristina la voce
            return
        threading.Thread(target=self._post_model, args=(model, provider, True),
                         daemon=True).start()

    # --- lingua (doppia via) ----------------------------------------------

    def _schedule_lang_poll(self) -> None:
        """Rilegge la lingua ogni secondo: il cambio può arrivare dalla VOCE
        (comando o auto-rilevamento) e il selettore deve seguirlo."""
        if self._win is None:
            return
        threading.Thread(target=self._load_language, daemon=True).start()
        self._lang_poll = self._win.after(1000, self._schedule_lang_poll)

    def _load_language(self) -> None:
        try:
            import requests
            d = requests.get(self._url + "/session/language", timeout=5).json()
        except Exception as e:  # noqa: BLE001 - bridge non raggiungibile
            log.debug("ModelPanel: lingua non letta: %s", e)
            return
        voci = d.get("languages") or []
        corrente = str(d.get("language") or "")
        self._overlay.post(lambda: self._fill_lang(voci, corrente))

    def _fill_lang(self, voci: list, corrente: str) -> None:
        if self._lang_combo is None:
            return
        codici = [str(v.get("code")) for v in voci]
        if codici != self._lang_codes:
            self._lang_codes = codici
            self._lang_combo["values"] = [
                f"{v.get('label') or v.get('code')} ({v.get('code')})" for v in voci]
        if corrente == self._lang_shown:
            return                      # nessun cambio: non toccare la selezione
        self._lang_shown = corrente
        if corrente in self._lang_codes:
            self._lang_combo.current(self._lang_codes.index(corrente))
        else:
            self._lang_combo.set("")    # nessuna sessione attiva

    def _on_lang_select(self, _evt=None) -> None:
        if self._lang_combo is None:
            return
        i = self._lang_combo.current()
        if not (0 <= i < len(self._lang_codes)):
            return
        code = self._lang_codes[i]
        if code == self._lang_shown:
            return
        self._lang_shown = code         # ottimistico: il poll conferma o corregge
        threading.Thread(target=self._post_language, args=(code,),
                         daemon=True).start()

    def _post_language(self, code: str) -> None:
        try:
            import requests
            r = requests.post(self._url + "/session/language",
                              json={"language": code}, timeout=10)
            if r.ok:
                log.info("ModelPanel: lingua -> %s", code)
            else:
                # Tipico: nessuna sessione voce attiva. Il poll rimetterà il
                # selettore su quello che vale davvero.
                log.info("ModelPanel: lingua non applicata (%s): %s",
                         r.status_code, (r.json() or {}).get("error", ""))
                self._lang_shown = ""
        except Exception as e:  # noqa: BLE001
            log.warning("ModelPanel: cambio lingua fallito: %s", e)
            self._lang_shown = ""

    def _on_new(self) -> None:
        threading.Thread(target=self._post_new, daemon=True).start()

    def _post_new(self) -> None:
        try:
            import requests
            requests.post(self._url + "/session/new", timeout=15)
            log.info("ModelPanel: nuova sessione")
        except Exception as e:  # noqa: BLE001
            log.warning("ModelPanel: nuova sessione fallita: %s", e)
