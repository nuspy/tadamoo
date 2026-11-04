"""What the agent's browser does about the things on a page that hide it: cookie and consent banners, permission prompts, JavaScript dialogs.

This is plumbing the harness runs by itself, by fixed rules; the model still decides what to read and do. Page content is untrusted: a page can
imitate a banner, so a click is made only on a control inside an element that is a known consent manager's, or a dialog or a pinned banner that
speaks of cookies or privacy, and only on a plain button (never a link that goes elsewhere, never a button that sends a form, never one whose
label asks for a payment, a sign-in or a download). The label the page puts on a control is matched against fixed phrases here; it is never
read as an instruction. Every click is reported to the model and recorded.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# "follow": the conversation's permission level decides (Auto: accept the banner, grant what a page asks; Manual: the necessary cookies only, ask the person)
CONSENT_POLICIES = ("follow", "necessary", "accept", "reject", "off")
PERMISSION_POLICIES = ("follow", "ask", "grant", "grant-safe", "deny")
DEFAULT_CONSENT = "follow"
DEFAULT_PERMISSIONS = "follow"

CONSENT_SECONDS = 60.0          # how long dealing with the banners of one page may take at most, so that a page that makes new ones for ever ends
LATE_SECONDS = 1.2              # how long to wait for a banner that loads late
LATE_HINT_SECONDS = 4.0         # ... when a consent manager's script is on the page but nothing shows yet
KNOWN_SITE_SECONDS = 0.4        # ... on a site where a banner was already dealt with
POLL_SECONDS = 0.3
GONE_SECONDS = 1.5              # how long a banner may take to go after a click (an animation) before it counts as still there


def clean_consent(value: Any) -> str:
    return value if value in CONSENT_POLICIES else DEFAULT_CONSENT


def clean_permissions(value: Any) -> str:
    return value if value in PERMISSION_POLICIES else DEFAULT_PERMISSIONS


# -- reading the page --------------------------------------------------------------------------------------

# Finds the banners: a known manager's container, or a dialog or pinned element that speaks of cookies/privacy (or of a newsletter, for a pop-up
# to close). Gives each button inside it (never a link that goes elsewhere or a button that sends a form) and keeps the elements in
# window.__bconsent for the click. Nothing is changed on the page.
DETECT_JS = r"""
(() => {
  const FRAME = %FRAME%;
  const out = { banners: [], hint: false };
  try {
    if (!/^https?:$/.test(location.protocol)) return JSON.stringify(out);
    const vw = innerWidth, vh = innerHeight;
    const WORDS = /cookie|consent|gdpr|rgpd|dsgvo|ccpa|privacy|privacidad|confidentialit|datenschutz|informativa|preferences|preferenze|tracking|trackers/i;
    const NEWS = /newsletter|subscribe|sign up|signup|iscriviti|iscrizione|abonn|inscri|suscri|anmeld/i;
    const NEC = /necessar|essenzial|essential|essentiel|strictly|required|obbligator|notwendig|erforderlich|esencial|technical|tecnic|always active|sempre attiv/i;
    const KNOWN = [
      ['onetrust', '#onetrust-banner-sdk,#onetrust-pc-sdk'], ['cookiebot', '#CybotCookiebotDialog'],
      ['didomi', '#didomi-popup,#didomi-notice,.didomi-popup-container'], ['quantcast', '.qc-cmp2-container,#qc-cmp2-container,#qc-cmp2-ui'],
      ['funding-choices', '.fc-consent-root,.fc-dialog-container'],
      ['trustarc', '#truste-consent-track,#truste-consent-content,#consent_blackbar,.truste_box_overlay,#teconsent'],
      ['usercentrics', '#usercentrics-root'], ['iubenda', '#iubenda-cs-banner,.iubenda-cs-container'],
      ['cookieyes', '.cky-consent-container,.cky-modal,#cookie-law-info-bar,#cliSettingsPopup'],
      ['complianz', '#cmplz-cookiebanner-container,.cmplz-cookiebanner'], ['osano', '.osano-cm-window,.osano-cm-dialog'],
      ['sourcepoint', '[id^=sp_message_container]'], ['consentmanager', '#cmpbox,#cmpwrapper'], ['google-search', '#CXQnmb'],
    ];
    const FRAME_HOST = /(^|\.)(consent\.google\.com|consent\.youtube\.com|privacy-mgmt\.com|sp-prod\.net|sourcepoint\.com|consentmanager\.net|cookiebot\.com|onetrust\.com|cookielaw\.org|trustarc\.com|truste\.com|quantcast\.com|quantserve\.com|didomi\.io|usercentrics\.eu|usercentrics\.com|iubenda\.com|cookieyes\.com|osano\.com|fundingchoices\.google\.com|consensu\.org)$/i;
    const SAFE_FORM_HOST = /^consent\.(google|youtube)\.com$/i;
    const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0.05; };
    const shownRoot = c => shown(c) || (c.shadowRoot && [...c.shadowRoot.children].some(shown));
    const semantic = e => e.matches('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]');
    const pinned = e => {
      for (let n = e, k = 0; n && n !== document.body && k < 8; n = n.parentElement, k++) { const p = getComputedStyle(n).position; if (p === 'fixed' || p === 'sticky') return true; }
      const s = getComputedStyle(e), r = e.getBoundingClientRect();
      return s.position === 'absolute' && e.parentElement === document.body && (r.top <= 24 || r.bottom >= vh - 24);
    };
    const nodes = [];
    window.__bconsent = nodes;
    const attrsOf = b => [b.id, typeof b.className === 'string' ? b.className : '', b.getAttribute('data-testid') || '', b.getAttribute('data-action') || ''].join(' ').slice(0, 200);
    const labelOf = b => (b.innerText || b.getAttribute('aria-label') || b.value || b.title || b.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const plainLink = a => { if (a.tagName !== 'A') return true; const h = a.getAttribute('href'); if (a.hasAttribute('download') || a.target === '_blank') return false; return h === null || h === '' || h.startsWith('#') || /^javascript:\s*(void\(0\)|;)?\s*$/i.test(h); };
    const formOk = b => {
      const f = b.form; if (!f) return true;
      const submits = b.tagName === 'BUTTON' ? b.type === 'submit' : (b.tagName === 'INPUT' && b.type === 'submit');
      if (!submits) return true;
      if (b.hasAttribute('formaction')) return false;
      if (f.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio]),textarea,select')) return false;
      try { return SAFE_FORM_HOST.test(new URL(f.action, location.href).hostname); } catch (x) { return false; }
    };
    const buttonsOf = c => {
      const root = c.shadowRoot || c, list = [];
      root.querySelectorAll('button,[role=button],input[type=button],input[type=submit],a').forEach(b => {
        if (nodes.includes(b) || b.disabled || b.getAttribute('aria-disabled') === 'true' || !shown(b) || !plainLink(b) || !formOk(b) || b.hasAttribute('formaction')) return;
        nodes.push(b);
        list.push({ i: nodes.length - 1, label: labelOf(b), attrs: attrsOf(b), tag: b.tagName.toLowerCase() });
      });
      return list;
    };
    const tickedIn = c => {
      let n = 0;
      (c.shadowRoot || c).querySelectorAll('input[type=checkbox]:checked,[role=switch][aria-checked=true],[role=checkbox][aria-checked=true]').forEach(x => {
        if (x.disabled || x.getAttribute('aria-disabled') === 'true') return;
        const l = x.closest('label');
        const text = (l ? l.innerText : '') + ' ' + (x.getAttribute('aria-label') || '') + ' ' + (x.parentElement ? x.parentElement.innerText.slice(0, 120) : '');
        if (!NEC.test(text)) n++;
      });
      return n;
    };
    const seen = new Set();
    const add = (name, c, known) => {
      if (seen.has(c)) return;
      seen.add(c);
      const buttons = buttonsOf(c);
      if (!buttons.length) return;
      const text = (c.innerText || '').slice(0, 3000);
      out.banners.push({ name, known, news: NEWS.test(text), ticked: tickedIn(c), buttons });
    };
    if (/^consent\.(google|youtube)\.com$/i.test(location.hostname) || (FRAME && FRAME_HOST.test(location.hostname))) {
      add('manager:' + location.hostname, document.body, true);
    }
    for (const [name, sel] of KNOWN) document.querySelectorAll(sel).forEach(c => { if (shownRoot(c)) add(name, c, true); });
    const cands = new Set(document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]'));
    [...document.body.children].forEach(c => { cands.add(c); [...c.children].forEach(g => cands.add(g)); });
    document.querySelectorAll('[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[id*=gdpr i],[class*=gdpr i]').forEach(c => cands.add(c));
    for (const c of cands) {
      if (!c.isConnected || c === document.body || c === document.documentElement || /^(MAIN|ARTICLE|NAV|HEADER|FORM|SCRIPT|STYLE)$/.test(c.tagName) || !shown(c)) continue;
      if (!(semantic(c) || pinned(c))) continue;
      const text = c.innerText || '';
      if (!text || text.length > 3000 || !(WORDS.test(text) || NEWS.test(text))) continue;
      add('banner', c, false);
    }
    out.hint = out.banners.length === 0 && !!document.querySelector(
      'script[src*=onetrust i],script[src*=cookiebot i],script[src*=didomi i],script[src*=quantcast i],script[src*=cookielaw i],script[src*=trustarc i],' +
      'script[src*=usercentrics i],script[src*=iubenda i],script[src*=cookieyes i],script[src*=complianz i],script[src*=osano i],script[src*=sourcepoint i],' +
      'script[src*=privacy-mgmt i],script[src*=consentmanager i],script[src*=fundingchoices i],' + KNOWN.map(k => k[1]).join(','));
  } catch (x) { /* the page broke the lookup: nothing is found */ }
  return JSON.stringify(out);
})()
"""

# Where to click a control found by DETECT_JS, or null when it moved, is hidden, or something else lies over it.
CLICK_POINT_JS = r"""
(() => {
  const e = (window.__bconsent || [])[%I%];
  if (!e || !e.isConnected) return null;
  e.scrollIntoView({block: 'nearest', inline: 'nearest'});
  const r = e.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return null;
  const x = r.left + r.width / 2, y = r.top + r.height / 2, root = e.getRootNode();
  const hit = (root.elementFromPoint ? root : document).elementFromPoint(x, y);
  if (!hit || !(hit === e || e.contains(hit) || hit.contains(e))) return null;
  return [x, y];
})()
"""


def detect_script(frame: bool = False) -> str:
    return DETECT_JS.replace("%FRAME%", "true" if frame else "false")


# -- what a control says -----------------------------------------------------------------------------------

def norm(text: str) -> str:
    """A label without accents, quotes, arrows and surrounding punctuation, in lower case, to be matched against fixed phrases."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("’", "'").replace("‘", "'").replace("ß", "ss")
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" .!:;>›»→✓✔*")


def _phrases(*parts: str) -> "re.Pattern[str]":
    return re.compile(r"^(?:" + "|".join(parts) + r")$")


_COOKIES = r"(?: (?:the |all |non-?essential |optional |these )?cookies?)?"
_REJECT = _phrases(
    r"reject(?: all)?" + _COOKIES, r"reject non-?essential" + _COOKIES, r"decline(?: all)?" + _COOKIES, r"deny(?: all)?" + _COOKIES,
    r"refuse(?: all)?" + _COOKIES, r"disagree", r"i (?:do not|don't) (?:accept|agree)", r"continue without (?:accepting|agreeing|consent)",
    r"rifiuta(?: tutto| tutti| i cookie)?", r"rifiuto", r"nega(?: tutto| tutti)?", r"non accetto", r"continua senza accettare", r"continua senza accettare",
    r"tout refuser", r"refuser(?: tout| tous)?(?: les cookies)?", r"continuer sans accepter", r"je refuse",
    r"alle ablehnen", r"ablehnen", r"alles ablehnen", r"ohne zustimmung (?:fortfahren|weiter)", r"weiter ohne zustimmung",
    r"rechazar(?: todo| todas| todas las cookies)?", r"rechazo", r"denegar(?: todo)?", r"continuar sin aceptar",
    r"rejeitar(?: tudo| todos)?", r"recusar(?: tudo| todos)?",
    r"(?:reject|decline|rifiuta|refuser|ablehnen|rechazar|rechaza)\b.{0,32}",
    r".*\b(?:only|solo|uniquement|seulement|nur|apenas|solamente)\b.*\b(?:necessar\w*|essential\w*|essenzial\w*|essentiel\w*|necessari\w*|notwendig\w*|erforderlich\w*|"
    r"esencial\w*|technical|tecnic\w*|required)\b.*",
    r".*\b(?:necessar\w*|essential\w*|essentiel\w*|essenzial\w*|notwendig\w*|erforderlich\w*|necesari\w*|esencial\w*|required)\b(?: cookies?)? (?:only|solo|nur|uniquement|seulement|apenas)\b.*",
    r".*\b(?:without|senza|sans|ohne|sin) (?:accept\w*|accett\w*|accepter|akzeptier\w*|aceptar|consent\w*|zustimm\w*)\b.*",
)
_ACCEPT = _phrases(
    r"accept(?: all)?" + _COOKIES + r"(?: (?:and|&) (?:continue|close|proceed))?", r"allow(?: all)?" + _COOKIES, r"i (?:accept|agree|consent)", r"agree(?: and (?:continue|close))?",
    r"got it", r"ok(?:ay)?(?:,? got it)?", r"understood", r"yes,? i(?:'m| am) happy", r"accept and continue",
    r"accetta(?: tutto| tutti| tutti i cookie| i cookie| e continua| e chiudi| e prosegui)?", r"accetto(?: tutto)?", r"acconsento", r"ho capito", r"consenti(?: tutto| tutti)?",
    r"va bene", r"d'accordo", r"accetta e continua",
    r"tout accepter", r"accepter(?: tout| tous)?(?: les cookies)?(?: et (?:continuer|fermer))?", r"j'accepte", r"d'accord",
    r"alle akzeptieren", r"akzeptieren(?: und (?:weiter|schliessen))?", r"alle cookies (?:akzeptieren|zulassen)", r"zustimmen(?: und weiter)?", r"einverstanden",
    r"ich stimme zu", r"alle zulassen", r"verstanden",
    r"aceptar(?: todo| todas| todas las cookies| y continuar| y cerrar)?", r"acepto", r"permitir(?: todo| todas| todas las cookies)", r"entendido", r"consentir", r"de acuerdo",
    r"aceitar(?: tudo| todos| e continuar)?", r"concordo",
    r"(?:accept|accetta|accepter|akzeptieren|aceptar|aceitar)\b.{0,32}",
)
_SETTINGS = _phrases(
    r"(?:cookie |cookies )?settings", r"manage(?: my| your)?(?: cookie| cookies)?(?: preferences| settings| choices| options)?", r"customi[sz]e(?: cookies| settings| choices| my choices)?",
    r"more options", r"preferences", r"options", r"set preferences", r"cookie preferences", r"privacy settings", r"your choices",
    r"personalizza(?: i cookie| le preferenze)?", r"gestisci(?: le mie| le)?(?: preferenze| cookie| impostazioni| opzioni)?", r"impostazioni(?: dei cookie| cookie)?", r"preferenze", r"opzioni",
    r"personnaliser(?: mes choix| les cookies)?", r"parametrer(?: les cookies| mes choix)?", r"parametres(?: des cookies)?", r"gerer(?: mes choix| mes preferences| les cookies)?",
    r"plus d'options", r"preferences",
    r"einstellungen", r"cookie-einstellungen", r"anpassen", r"mehr optionen", r"individuell anpassen", r"konfigurieren",
    r"configurar(?: cookies)?", r"gestionar(?: preferencias| cookies| opciones)?", r"personalizar(?: cookies)?", r"mas opciones", r"configuracion(?: de cookies)?",
)
_SAVE = _phrases(
    r"save", r"save (?:my )?(?:settings|preferences|choices|selection)(?: and (?:close|continue))?", r"confirm(?: my)?(?: choices| selection| preferences)",
    r"allow selection", r"accept (?:selected|selection)", r"confirm",
    r"salva(?: le preferenze| le mie scelte| impostazioni| e chiudi)?", r"conferma(?: le mie scelte| le preferenze| la selezione)?", r"consenti la selezione", r"accetta la selezione",
    r"enregistrer(?: mes choix| mes preferences)?", r"valider(?: mes choix)?", r"confirmer(?: mes choix)?", r"accepter la selection",
    r"auswahl (?:speichern|bestaetigen|erlauben|akzeptieren)", r"einstellungen speichern", r"meine auswahl (?:bestaetigen|speichern)", r"speichern",
    r"guardar(?: preferencias| mis preferencias| seleccion| configuracion)?", r"confirmar(?: mis opciones| seleccion| mi seleccion)?", r"aceptar seleccion", r"permitir seleccion",
)
_CLOSE = _phrases(
    r"x", r"×", r"✕", r"✖", r"╳", r"close(?: dialog| popup| pop-up| modal| window)?", r"dismiss", r"no,? thanks?", r"not now", r"maybe later",
    r"chiudi", r"no,? grazie", r"non ora", r"fermer", r"non merci", r"plus tard", r"schliessen", r"nein,? danke", r"cerrar", r"no,? gracias", r"ahora no",
)
# what a control may never ask for, however it is worded: payment, sign-in, sign-up, download
_DENIED = re.compile(
    r"\b(?:pay|payment|buy|purchase|order|checkout|check out|subscribe|subscription|sign ?in|log ?in|login|log ?on|register|sign ?up|create account|download|install|upgrade|"
    r"premium|donate|paga|pagamento|acquista|compra|ordina|abbonati|abbonamento|accedi|registrati|iscriviti|scarica|installa|dona|payer|acheter|commander|abonner|"
    r"connexion|connecter|inscrire|telecharger|installer|kaufen|bezahlen|bestellen|abonnieren|anmelden|registrieren|herunterladen|installieren|spenden|pagar|comprar|"
    r"suscribirse|suscribete|iniciar sesion|registrarse|descargar|instalar|donar)\b")
_ATTR_ROLES = (
    ("reject", re.compile(r"reject-?all|reject_all|rejectall|btn-reject|reject-btn|disagree|deny-?all|deny_all|osano-cm-deny|cmplz-deny|button-?decline|ButtonDecline|"
                          r"truste-consent-required|W0wltc|cs-reject-btn|cookie_action_close_header_reject", re.I)),
    ("accept", re.compile(r"accept-btn|accept-all|accept_all|acceptall|allow-?all|allowall|agree-button|notice-agree|btn-accept|cmplz-accept|osano-cm-accept|cs-accept-btn|"
                          r"truste-consent-button|cookie_action_close_header(?!_reject)|wt-cli-accept|L2AGLb|AllowAll", re.I)),
    ("settings", re.compile(r"pc-btn-handler|learn-more-button|uc-more-button|customi[sz]e|view-preferences|cs-customize-btn|osano-cm-manage|ButtonDetails|btn-settings|"
                            r"settings-btn|cky-btn-preferences", re.I)),
    ("save", re.compile(r"save-preference|save-button|uc-save-button|save-settings|cmplz-save|osano-cm-save|AllowallSelection|btn-save|confirm-choices", re.I)),
)
_CLOSE_ATTR = re.compile(r"close|dismiss", re.I)


def classify(label: str, attrs: str = "", news: bool = False) -> str:
    """What a control is for: reject, accept, settings, save, close, or empty when it is none of those or asks for something it may not.
    A close control counts only in a pop-up for a newsletter: on a consent banner it is ambiguous, so it is left alone."""
    text = norm(label)
    if len(text) > 60 or _DENIED.search(text):
        return ""
    for role, pattern in (("reject", _REJECT), ("accept", _ACCEPT), ("settings", _SETTINGS), ("save", _SAVE)):
        if pattern.match(text):
            return role
    if news and (_CLOSE.match(text) or (len(re.sub(r"\w", "", text)) == len(text) and _CLOSE_ATTR.search(attrs or ""))):
        return "close"
    if not text or len(text) <= 2:                               # an icon, a mark: the manager's own id says what it is
        for role, pattern in _ATTR_ROLES:
            if pattern.search(attrs or ""):
                return role
    return ""


def safe_label(label: str, limit: int = 40) -> str:
    """A page's wording for a control, as it is shown to the model and recorded: one line, plain, short."""
    text = re.sub(r"[\x00-\x1f\x7f\"\\]+", " ", str(label or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass
class Pick:
    banner: Dict[str, Any]
    button: Dict[str, Any]
    role: str

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.banner.get("name", ""), self.role, norm(self.button.get("label", "")))


ORDER = {"accept": ("accept",), "reject": ("reject", "save", "settings"), "necessary": ("reject", "save", "settings", "accept")}


def roles_of(banner: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for b in banner.get("buttons", []):
        role = classify(b.get("label", ""), b.get("attrs", ""), banner.get("news", False))
        if role:
            out.setdefault(role, []).append(b)
    return out


def decide(policy: str, banners: List[Dict[str, Any]], tried: set) -> Tuple[Optional[Pick], Optional[str]]:
    """The one control to click next, by the policy, and, when a consent banner is there but the policy leaves it alone, why."""
    if policy not in ORDER:
        return None, None
    left: Optional[str] = None
    for banner in banners:
        roles = roles_of(banner)
        if not any(r in roles for r in ("accept", "reject", "settings", "save")):
            continue
        for role in ORDER[policy]:
            if role == "save" and banner.get("ticked"):
                continue                                          # a save with options ticked would give consent
            for button in roles.get(role, []):
                pick = Pick(banner, button, role)
                if pick.key not in tried:
                    return pick, None
        left = {"accept": "it has no accept control", "reject": "it has no reject control that can be used"}.get(policy, "no control that can be used was found")
    for banner in banners:                                        # a newsletter pop-up: closed, and nothing else in it
        roles = roles_of(banner)
        if roles.get("close") and not any(r in roles for r in ("accept", "reject", "settings", "save")):
            pick = Pick(banner, roles["close"][0], "close")
            if pick.key not in tried:
                return pick, None
    return None, left


def _signature(banners: List[Dict[str, Any]]) -> Tuple:
    return tuple((b.get("name"), tuple(norm(x.get("label", "")) for x in b.get("buttons", []))) for b in banners)


@dataclass
class ConsentResult:
    clicks: List[Dict[str, str]]
    unresolved: str = ""


def run_consent(policy: str, detect: Callable[[], Dict[str, Any]], click: Callable[[Pick], bool], patience: float = LATE_SECONDS,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> ConsentResult:
    """Looks for a banner and acts on it by the policy. `detect` gives {"banners": [...], "hint": bool} for the page now; `click` makes the click on
    a control and says whether it was made. A banner that loads late is waited for; after a click the banner is given time to go before it is
    taken as still there (then the next control by the policy is tried: for `necessary`, the accept control comes last)."""
    result = ConsentResult([])
    if policy not in ORDER:
        return result
    tried: set = set()
    start = clock()
    while clock() - start < CONSENT_SECONDS:
        state = detect()
        banners = state.get("banners") or []
        pick, left = decide(policy, banners, tried) if banners else (None, None)
        if pick is None:
            if banners:
                result.unresolved = left or ""
                break
            if result.clicks:
                break
            if clock() - start >= max(patience, LATE_HINT_SECONDS if state.get("hint") else 0.0):
                break
            sleep(POLL_SECONDS)
            continue
        tried.add(pick.key)
        before = _signature(banners)
        if not click(pick):
            continue
        result.clicks.append({"label": safe_label(pick.button.get("label", "")), "role": pick.role, "banner": str(pick.banner.get("name", ""))})
        end = clock() + GONE_SECONDS
        while clock() < end:
            sleep(POLL_SECONDS)
            if _signature(detect().get("banners") or []) != before:
                break
    return result


def click_note(click: Dict[str, str], policy: str) -> str:
    if click["role"] == "close":
        return f'pop-up: closed "{click["label"]}" (policy {policy})'
    return f'consent banner: clicked "{click["label"]}" (policy {policy})'


def consent_notes(result: ConsentResult, policy: str) -> List[str]:
    notes = [click_note(c, policy) for c in result.clicks]
    if any(c["role"] != "close" for c in result.clicks):
        notes.append("The harness dealt with the page's consent banner by its policy, so the page can be read; you do not need to click it.")
    if result.unresolved:
        state = "is still on the page" if result.clicks else "is on the page and the harness left it alone"
        notes.append(f"A consent banner {state} (policy {policy}: {result.unresolved}). If it hides what you need, deal with it using the click tools.")
    return notes


# -- permissions -------------------------------------------------------------------------------------------

# What the page may be refused before it asks: the DevTools names of the permissions that reveal the person, reach a device or open a prompt.
DENIED_PERMISSIONS = (
    "notifications", "geolocation", "audioCapture", "videoCapture", "displayCapture", "clipboardReadWrite", "clipboardSanitizedWrite", "midi", "midiSysex",
    "paymentHandler", "durableStorage", "sensors", "protectedMediaIdentifier", "idleDetection", "windowManagement", "localFonts", "backgroundSync",
    "periodicBackgroundSync", "backgroundFetch", "wakeLockScreen", "wakeLockSystem", "ar", "vr", "cameraPanTiltZoom", "nfc", "storageAccess", "topLevelStorageAccess",
    "capturedSurfaceControl", "keyboardLock", "speakerSelection", "webPrinting", "handTracking", "smartCard", "pointerLock", "automaticFullscreen",
)
SAFE_GRANTS = ("clipboardSanitizedWrite",)        # grant-safe: writing to the clipboard on a click; reading it, and everything else, stays refused

# The same refusals as the browser's own default content settings, so that no prompt is shown even where the protocol call is not available.
DEFAULT_CONTENT_SETTINGS = {
    "notifications": 2, "geolocation": 2, "media_stream_mic": 2, "media_stream_camera": 2, "midi_sysex": 2, "clipboard": 2, "usb_guard": 2, "serial_guard": 2,
    "hid_guard": 2, "bluetooth_guard": 2, "sensors": 2, "payment_handler": 2, "ar": 2, "vr": 2, "idle_detection": 2, "window_placement": 2, "file_system_write_guard": 2,
}
LAUNCH_FLAGS = ("--deny-permission-prompts",)
# flags that would answer a prompt for the person; none is ever passed
FORBIDDEN_FLAG_PARTS = ("use-fake-ui", "use-fake-device", "auto-select", "auto-accept", "allow-running-insecure", "unsafely-treat", "enable-media-stream",
                        "use-file-for-fake", "disable-web-security", "grant-", "allow-file-access", "enable-usermedia")


def write_permission_prefs(profile_dir: str | Path) -> None:
    """Sets the browser's default for each permission to refused, in the profile the browser starts with. Best effort: the protocol call is the main one."""
    path = Path(profile_dir) / "Default" / "Preferences"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
        values = data.setdefault("profile", {}).setdefault("default_content_setting_values", {})
        values.update(DEFAULT_CONTENT_SETTINGS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass


# What a page does to ask: noted (not answered) so that the person can be asked by the `ask` policy, or the model told by the others.
PERMISSION_WATCH_JS = r"""
(() => {
  const tell = kind => { try { window.__tadamooPerm(String(kind)); } catch (e) {} };
  const wrap = (owner, name, kind) => {
    try {
      const f = owner && owner[name]; if (typeof f !== 'function') return;
      owner[name] = function (...a) { tell(typeof kind === 'function' ? kind(a) : kind); return f.apply(this, a); };
    } catch (e) {}
  };
  wrap(window.Notification, 'requestPermission', 'notifications');
  if (window.Geolocation) { wrap(Geolocation.prototype, 'getCurrentPosition', 'location'); wrap(Geolocation.prototype, 'watchPosition', 'location'); }
  if (window.MediaDevices) {
    wrap(MediaDevices.prototype, 'getUserMedia', a => { const c = a[0] || {}; return c.video && c.audio ? 'camera and microphone' : (c.video ? 'camera' : 'microphone'); });
    wrap(MediaDevices.prototype, 'getDisplayMedia', 'screen capture');
  }
  if (window.Clipboard) { wrap(Clipboard.prototype, 'readText', 'clipboard'); wrap(Clipboard.prototype, 'read', 'clipboard'); }
  wrap(Navigator.prototype, 'requestMIDIAccess', 'MIDI');
  if (window.USB) wrap(USB.prototype, 'requestDevice', 'USB');
  if (window.Serial) wrap(Serial.prototype, 'requestPort', 'serial port');
  if (window.HID) wrap(HID.prototype, 'requestDevice', 'HID');
  if (window.StorageManager) wrap(StorageManager.prototype, 'persist', 'persistent storage');
})();
"""
WATCH_BINDING = "__tadamooPerm"

# what a request names -> the DevTools permissions that a person's approval turns on for that one site. A device chooser (USB, serial, HID) cannot be
# answered from here, so it is never granted.
KIND_TO_PERMISSIONS: Dict[str, Tuple[str, ...]] = {
    "notifications": ("notifications",), "location": ("geolocation",), "camera": ("videoCapture",), "microphone": ("audioCapture",),
    "camera and microphone": ("videoCapture", "audioCapture"), "screen capture": ("displayCapture",), "clipboard": ("clipboardReadWrite", "clipboardSanitizedWrite"),
    "MIDI": ("midi", "midiSysex"), "persistent storage": ("durableStorage",), "USB": (), "serial port": (), "HID": (),
}


def dialog_accepts(kind: str, leaving: bool, everything: bool = False) -> bool:
    """Whether a JavaScript dialog is accepted. An alert is only read and always accepted; a question or a prompt is accepted when the permission
    level lets the page have what it asks (`everything`; otherwise the person is asked first, see Browser._dialog_ok); leaving a page with a warning is
    accepted when navigating away was asked for or `everything`."""
    if kind == "alert":
        return True
    if kind == "beforeunload":
        return bool(leaving or everything)
    return bool(everything)


def dialog_note(kind: str, message: str, accepted: bool) -> str:
    if kind == "beforeunload":
        done = "left the page" if accepted else "stayed on the page"
    elif kind == "prompt":
        done = "accepted with its default text" if accepted else "dismissed (nothing was entered)"
    else:
        done = "accepted" if accepted else "dismissed"
    text = safe_label(message, 80)
    return f'The page showed a {kind} dialog{(" saying " + chr(34) + text + chr(34)) if text else ""}: {done}.'


def permission_note(kind: str, site: str, outcome: str) -> str:
    return f"The page asked for {kind} on {site}: {outcome}."
