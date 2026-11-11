# Keeping archives up to date: sources, check, update, verify

An archive can follow the places its documents came from. A **check** says what changed without reading any document; an **update** reads again only
what changed and puts it in the archive in one step; a **verify** says whether the archive is whole and still matches its sources. Nothing is ever lost
silently: a document that cannot be read keeps its old version and the report says why, a document its source no longer has is marked and the person is
asked, and the page shows what each reading missed. Code: `tadamoo/rag/sources/`, `serve/rag_sync_api.py`, `serve/web/ragsync.js`.

## Sources

`sources.json` in the archive's folder holds the sources, which document is which item of which source with the stamp it had when it was read (`links`), the
last check and update, the versions kept, and `attention`. `meta.json` is touched only when a document's content changes, so a check never makes the
archive reload. Reading a source adds nothing until an update; a document the archive already holds with the same text (SHA-256) is *linked* instead of
added twice, so a file added by hand can be followed afterwards.

| Kind | Settings | The cheap question (the stamp) | New, changed, gone |
|---|---|---|---|
| `file` | full path | modification time and size; when they moved the file is hashed, and the same content is not read again | the file is not there: gone |
| `folder` | full path, `recursive` | the same for every readable file; hidden files and folders are skipped, the data folder is never followed | new files appear; a removed file is gone; at most 1000 files, more is refused with the reason |
| `url` | http(s) address, no password | the ETag and Last-Modified are sent back as conditional headers: unchanged costs a 304 and no body; without them the page is downloaded and hashed | 404 and a server that is down are errors, never a removal |
| `mediawiki` | the wiki's `api.php` or a page address, `titles`, `category` | one batched question for up to 50 pages, the id of the latest revision; a redirect is followed (a target that moves is a change); the page is read at that revision | a missing page is reported; a category brings its new pages |
| `wikibridge` | address (empty: the one of Settings, Memories, Wiki), project, `sections` | see below | see below |

A web page is read as HTML or the file it is, through the ordinary ingestion; Wikipedia pages are read with `action=parse` and turned into a small HTML
file. Web text is untrusted data: every passage goes through the same screen for instructions to an assistant as a file does (`flag`), and the answers of the
tools are marked untrusted.

### The company wiki

The wiki service (wikibridge, `tadamoo/integrations/wikibridge_client.py`) has no change feed and no ETag. What it offers is `GET /wiki/tree`: every
page the key may see, with its title and `updated`, the time its content last changed (the wiki moves it only when the content changes). So:

- **check**: one call. A page whose `updated` or title differs from the recorded one is *changed*, a page not listed before is *new*, a page no longer listed is *gone*.
  No page is read. This is the cheapest correct polling the service allows.
- **update**: only the changed and new pages are read (`GET /wiki/page`, markdown and front matter); the hash of the text settles the rest, so a page whose
  `updated` moved over the same text is not replaced. The page's `content_version` is shown as its version.
- **gone** can mean deleted, renamed or no longer visible to this key: the wiki answers 404 for all three, so it is only marked.
- A wiki that does not answer, or that lists no page at all although pages were read from it before, is a failed check, not a removal.
- The wiki's own import of its sources (its admin `POST /update`) is not called: a refresh reads what the wiki holds now.

## Check and update

`check` gives per document: `unchanged`, `changed`, `new`, `gone`, `error` (the source could not be read: nothing is concluded about its documents), `pinned`
(held at an older version on purpose). `update` re-reads the changed and new items, and reports `updated` (with passages added, removed and kept), `added`,
`linked`, `failed` (the old version stays, with the reason), `gone`. `dry_run` does everything except save and gives, for each document, the passages that
would be added and removed with a few examples; the page calls it "What changed?".

How a changed document is replaced: it is fetched into a temporary file, read with `read_document`, cleaned and cut with the store's own cutting
(`RagStore.passages_of`), and its coverage is worked out. The passages that did not change are found by the hash of their text and keep their vectors; only the
rest is embedded. Then `RagStore.swap_documents` replaces all the documents of the run in **one commit** under the store's lock, against the archive as it is
then (a document added meanwhile is kept): the saved readings go first, `chunks.jsonl`, `sections.jsonl` and `embeddings.npy` next and `meta.json` last, each by
a temporary name and a rename; if anything fails the saved readings are put back and the archive is as it was. Vectors are kept only when every passage has
one of the same embedding; otherwise the archive is searched by words until "Index again", and the note says so. The document keeps its id; its meta gains
`updated`, `revision` and `origin` (`kind`, `ref`, `version`), shown under the document on the page.

A cancel or a failure in the run changes nothing that was not already committed whole. One run at a time per archive (`busy`).

## Versions

Before a document is replaced its saved reading is copied to `versions/<doc>/<n>.json.gz`. `keep_versions` (3) and `keep_days` (90) bound them. **Put back**
cuts an older reading again and replaces the current one, which becomes a version in its turn; the document is then **pinned**: a check shows it as `pinned`
and an update leaves it until "Update to the latest" (`force`) is pressed.

## Gone documents

`resolve_gone(doc, "keep")` stops the check asking (it asks again if the item comes back and goes again); `"remove"` removes the document from the archive. A
scheduled update never removes.

## Triggers

- **The page** (Knowledge, an archive): Check for changes, What changed?, Update all changed (n), per item Update / Keep / Remove, Verify, the sources, the versions,
  the schedule and the settings. The Knowledge tab flashes when a check or update leaves something new that needs the person (`/api/attention`, key `rag`): changes found,
  a document that failed, a source that could not be read.
- **A routine** (`docs/orchestration.md`): `deliver_to: {kind: "knowledge", ref: <archive id>}` with the message `check` or `update`. No model is woken and no
  conversation is made; the run's outcome is `nothing_new` or `progress` with the summary. "Check this archive every night" is `daily 03:00` and `check`.
- **A tool**: `knowledge_check(archive)` and `knowledge_update(archive, include_new?)` (`tadamoo/rag/sync_tools.py`). They ask the server, which holds the embedding
  model and the guards, and follow its job. `knowledge_check` is pure and in every role; `knowledge_update` is reversible (it asks below Auto, as a change does) and in the
  build and debug roles. Results are untrusted data; an archive not cleared for the cloud is not shown to a model outside this PC.

An update **waits** while a heavy job has the local model out (`docs/heavy-swap.md`), up to `wait_minutes` (20); a scheduled one is then skipped and tried at the
next time. A check needs no GPU and is never held.

## Politeness

`tadamoo/rag/sources/net.py`: a User-Agent that names the program and, if set, a contact (`contact`); a pause between requests to one site (`min_interval_s`,
1 s; a site on this PC or network is not made to wait); robots.txt for addresses (`respect_robots`; not for MediaWiki, whose API etiquette is the User-Agent,
`maxlag`, one request at a time and the retry-after wait); conditional requests; gzip; at most 5 redirects, http and https only; a size cap (`max_download_mb`, 50) and a
time cap per request (`request_timeout_s`, 30) and per run (`max_run_minutes`, 30); one retry after the wait a site asks for with 429 or 503 (at most 30 s).
Settings: `sources.json` in the folder of archives, `GET/POST /api/rag/sync/settings`.

## Coverage and verify

Every reading gets `coverage` in its document's meta (`tadamoo/rag/sources/coverage.py`): pages the document has, pages some passage covers, pages that came out
empty (`empty_pages`) or could not be read (`skipped_pages`), pages no passage covers and nobody explains, passages flagged, a status (`ok`, `check`, `incomplete`)
and the problems in words. The page shows the sum for the archive and the problems per document.

`verify` (a job) changes nothing and lists problems by level (`error`, `warn`, `info`): the passages in the file against the count in `meta.json` and per document,
orphan passages, the vectors against the passages, every saved reading readable, each coverage record recomputed from the passages, the sources (changed, new, gone,
unreachable) and the documents that came from no source. `deep` also cuts the saved readings again and compares them with the stored passages (when the cutting
versions agree), hashes the files of local sources and counts the pages of local PDFs again.

## Routes

Under `/api/rag` (this PC only); the job routes are the ones of adding a document.

| Route | |
|---|---|
| `GET archives/<id>/sync` | sources, links (document, item, version, pinned), last check and update, attention, history, settings, coverage |
| `POST archives/<id>/sources` | `{kind, label?, ...settings}` follows a source (nothing is read); `.../sources/<sid>/remove` `{remove_documents?}` |
| `POST archives/<id>/check` | `{sources?}`: `{job}`, the result is the report |
| `POST archives/<id>/refresh` | `{only?, include_new?, dry_run?, force?}`: `{job}` |
| `POST archives/<id>/verify` | `{deep?}`: `{job}` |
| `POST archives/<id>/documents/<doc>/gone` | `{action: remove \| keep}` |
| `GET archives/<id>/documents/<doc>/versions`, `POST .../rollback` | the kept versions; `{version}`: `{job}` |
| `GET/POST sync/settings` | `keep_versions`, `keep_days`, `min_interval_s`, `max_download_mb`, `request_timeout_s`, `max_run_minutes`, `wait_minutes`, `respect_robots`, `contact` |

## Not done

Citations in answers do not show the source's version or date (it is in the document's meta). The enrichment of changed passages by a model (a separate job) is not run
again by an update. A refresh does not ask the company wiki to re-read its own sources. Tests: `tests/test_rag_sources_*.py`, `serve/tests/test_rag_sync_page.py`.
