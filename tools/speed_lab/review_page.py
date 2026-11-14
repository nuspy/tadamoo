"""A review page for videos: for each row the videos side by side and played together, the numbers, a field for notes, and a button that writes a .md with only the rows that were given a note.

    from review_page import build
    build(out_html, title, intro, rows)         rows: [{"id", "title", "what", "look", "numbers": {label: value}, "videos": [{"label", "src"}]}]

`src` is a path relative to the page. A row with no note counts as passed. The page keeps what is typed in the page's own storage so a reload does not lose it. Mobile first: one column under 700 px, targets of 44 px."""
from __future__ import annotations

import html
import json
from pathlib import Path

CSS = """
:root{--bg:#10151c;--panel:#18212c;--text:#e8edf3;--mut:#9fb0c3;--line:#2a3745;--acc:#5aa9ff;--ok:#4ac28a}
@media (prefers-color-scheme: light){:root{--bg:#f4f6f9;--panel:#fff;--text:#16202b;--mut:#51606f;--line:#d5dce4;--acc:#1b6fd6;--ok:#14804f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,Segoe UI,sans-serif;padding:0 16px 80px}
h1{font-size:1.4rem;margin:16px 0 4px}p.intro{color:var(--mut);max-width:70ch}
.bar{position:sticky;top:0;background:var(--bg);padding:10px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center;z-index:5;border-bottom:1px solid var(--line)}
button{min-height:44px;padding:0 16px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;font:inherit;cursor:pointer}
button.alt{background:transparent;color:var(--acc)}
.row{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin:16px 0}
.row h2{font-size:1.1rem;margin:0 0 4px}.what,.look{color:var(--mut);margin:2px 0}
.vids{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:10px;margin:10px 0}
figure{margin:0}figcaption{font-size:.9rem;color:var(--mut);padding:4px 0}video{width:100%;border-radius:8px;background:#000}
table.num{border-collapse:collapse;font-size:.9rem;margin:8px 0;width:100%}table.num td{padding:3px 8px;border-bottom:1px solid var(--line)}table.num td:first-child{color:var(--mut)}
textarea{width:100%;min-height:72px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text);font:inherit;padding:8px}
.rowbar{display:flex;gap:8px;margin:6px 0}#msg{color:var(--ok)}
"""

JS = """
const rows=[...document.querySelectorAll('.row')];
function vids(r){return [...r.querySelectorAll('video')]}
function playRow(r){const v=vids(r);v.forEach(x=>{x.currentTime=0});v.forEach(x=>x.play())}
function stopAll(){document.querySelectorAll('video').forEach(x=>x.pause())}
rows.forEach(r=>{
  const v=vids(r);
  r.querySelector('.play').onclick=()=>playRow(r);
  r.querySelector('.pause').onclick=()=>v.forEach(x=>x.pause());
  v.forEach(x=>{x.addEventListener('seeked',()=>{if(x.dataset.lock)return;v.forEach(y=>{if(y!==x&&Math.abs(y.currentTime-x.currentTime)>0.08){y.dataset.lock=1;y.currentTime=x.currentTime;setTimeout(()=>delete y.dataset.lock,150)}})});
    x.addEventListener('play',()=>v.forEach(y=>{if(y!==x&&y.paused)y.play()}));x.addEventListener('pause',()=>v.forEach(y=>{if(y!==x&&!y.paused)y.pause()}))});
  const t=r.querySelector('textarea');try{t.value=localStorage.getItem(PAGE+r.id)||''}catch(e){}
  t.oninput=()=>{try{localStorage.setItem(PAGE+r.id,t.value)}catch(e){}};
});
document.getElementById('all').onclick=()=>{let i=0;const next=()=>{if(i>=rows.length)return;const r=rows[i++];r.scrollIntoView({block:'center'});const v=vids(r);if(!v.length)return next();playRow(r);v[0].onended=next};next()};
document.getElementById('stop').onclick=stopAll;
document.getElementById('publish').onclick=()=>{
  const flagged=rows.filter(r=>r.querySelector('textarea').value.trim());
  let md='# '+TITLE+' — righe segnalate\\n\\n';
  if(!flagged.length)md+='Nessuna riga segnalata: tutto superato.\\n';
  flagged.forEach(r=>{md+='## '+r.dataset.title+'\\n\\n- Cosa è stato fatto: '+r.dataset.what+'\\n- Numeri: '+r.dataset.numbers+'\\n- Nota: '+r.querySelector('textarea').value.trim().replace(/\\n/g,'\\n  ')+'\\n\\n'});
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([md],{type:'text/markdown'}));a.download=FILE;a.click();
  document.getElementById('msg').textContent=flagged.length+' righe segnalate nel file '+FILE;
};
"""


def build(out: Path, title: str, intro: str, rows: list, md_name: str = "segnalate.md") -> Path:
    cards = []
    for r in rows:
        nums = r.get("numbers") or {}
        table = "<table class='num'>" + "".join(f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>" for k, v in nums.items()) + "</table>" if nums else ""
        figs = "".join(f"<figure><video src='{html.escape(v['src'])}' controls preload='metadata' playsinline muted loop></video><figcaption>{html.escape(v['label'])}</figcaption></figure>" for v in r.get("videos", []))
        flat = "; ".join(f"{k} {v}" for k, v in nums.items())
        cards.append(f"<section class='row' id='{html.escape(r['id'])}' data-title='{html.escape(r['title'], quote=True)}' data-what='{html.escape(r.get('what', ''), quote=True)}' data-numbers='{html.escape(flat, quote=True)}'>"
                     f"<h2>{html.escape(r['title'])}</h2><p class='what'>{html.escape(r.get('what', ''))}</p><p class='look'>{html.escape(r.get('look', ''))}</p>"
                     f"<div class='vids'>{figs}</div>{table}<div class='rowbar'><button class='play'>Riproduci insieme</button><button class='alt pause'>Pausa</button></div>"
                     f"<textarea placeholder='Note (vuoto = superato)' aria-label='Note per {html.escape(r['title'])}'></textarea></section>")
    page = (f"<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
            f"<h1>{html.escape(title)}</h1><p class='intro'>{html.escape(intro)}</p>"
            f"<div class='bar'><button id='all'>Riproduci tutte di seguito</button><button class='alt' id='stop'>Ferma</button><button id='publish'>Pubblica</button><span id='msg' role='status'></span></div>"
            f"{''.join(cards)}<script>const TITLE={json.dumps(title)};const FILE={json.dumps(md_name)};const PAGE={json.dumps(Path(out).parent.name + '/')};{JS}</script></body></html>")
    Path(out).write_text(page, encoding="utf-8")
    return Path(out)
