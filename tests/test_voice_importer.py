"""The import of a callAPIcall folder (voice/importer.py through serve/voice_import.py): a plan that writes nothing, personalities made with their
memory encrypted, an archive linked to them, clips copied and the cache left, conflicts, personal data that needs a confirmation, a second run that
changes nothing. The folders are built in a temporary place; the real callAPIcall data is never read."""
import json
import os
import shutil
import wave
from pathlib import Path

import pytest

from serve import voice_import
from tadamoo import personas as pe
from tadamoo.rag.store import RagStore

PROFILE_1 = "She likes the early morning and the mountains behind the lake house."
PROFILE_2 = "She dislikes loud meetings and always asks for the agenda first."
SESSION = "Planned the spring trip with a long stop at the old observatory."
DIARY = ("Dear diary, today the senate met in the old forum and we spoke about the grain, the roads and the new bridge over the river. " * 12)


def _write(path: Path, data, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        data = json.dumps(data)
    path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data, encoding="utf-8")


def _tone(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 1600)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "callAPIcall"
    people = root / "avatar" / "persone"
    _write(people / "Shan" / "manifest.json", {"name": "Shan", "voice": {"engine": "xtts", "voice": "Chandra MacFarland"}, "clips": ["a.mp4"],
                                                "personality": "Warm and a little ironic.", "system_prompt": "You are Shan.", "knowledge_project": "wiki-shan"})
    _write(people / "Shan" / "memory.json", {"version": 1, "last_seen_ts": 1760000000.0, "rolling_profile": PROFILE_1 + "\n\n" + PROFILE_2,
                                              "recent_sessions": [{"ts": 1760000000.0, "title": "Spring trip", "summary": SESSION}, {"ts": 1760100000.0, "title": "", "summary": "Short call."}],
                                              "stats": {"sessions": 2}})
    _write(people / "Shan" / "clips" / "a.mp4", b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000)
    _write(people / "Shan" / "poster.jpg", b"\xff\xd8\xff" + b"p" * 400)
    _write(people / "Shan" / "wav2lip" / "cache.bin", b"c" * 2_000_000)
    _write(people / "Giulio_Cesare" / "manifest.json", {"name": "Giulio_Cesare", "voice": {"engine": "dia", "voice": "Pluto"}, "knowledge_project": "",
                                                        "personality_from_docs": "A general who writes of his own wars.", "personality": "", "system_prompt": "You are Caesar."})
    _write(people / "Giulio_Cesare" / "docs" / "diary.txt", DIARY)
    _write(people / "Giulio_Cesare" / "rag" / "chunks.jsonl", json.dumps({"id": "c1", "source": "diary.txt", "text": "from the saved passages"}) + "\n")
    _write(people / "Giulio_Cesare" / "rag" / "embeddings.npy", b"\x93NUMPY")
    _write(people / "Andrea_Taini" / "manifest.json", {"name": "Andrea_Taini", "voice": {"engine": "kokoro", "voice": "im_nicola"}, "clips": []})
    _write(people / "Andrea_Taini" / "clips" / "me.mp4", b"\x00\x00\x00\x18ftypmp42" + b"m" * 3000)
    _write(people / "Vanessa" / "manifest.json", {"name": "Vanessa", "voice": {"engine": "kokoro", "voice": "if_sara"}})
    _write(people / "Broken" / "memory.json", {"rolling_profile": "x"})
    _write(people / "Passages" / "manifest.json", {"name": "Passages", "voice": {}})
    _write(people / "Passages" / "rag" / "chunks.jsonl", "".join(json.dumps({"id": f"c{i}", "source": "book.pdf", "text": f"Passage number {i} about the cold winter in the north and the long road."}) + "\n" for i in range(3)))
    _tone(root / "cloned_voices" / "sara.wav")
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TADAMOO_CACHE", str(tmp_path / "cache"))
    voice_import.userdata.path("voice")                                   # the data folder and its one-off marker exist before the tests look
    store = pe.PersonaStore(tmp_path / "data" / "personas", key_dir=tmp_path / "data" / "vault")
    rag = RagStore(tmp_path / "data" / "rag", forbidden=[tmp_path / "data"])
    voices = tmp_path / "models" / "tts" / "voices"
    monkeypatch.setattr(voice_import.voice_settings, "models_dir", lambda: str(tmp_path / "models"))
    return type("Env", (), {"store": store, "rag": rag, "voices": voices, "avatars": tmp_path / "data" / "voice" / "avatars", "data": tmp_path / "data"})


def _plan(env, source):
    return voice_import.plan({"source": str(source)}, env.store, env.rag)


def _run(env, source, items):
    return voice_import.run({"source": str(source), "confirm": True, "items": items}, env.store, env.rag)


def _by(plan):
    return {i["id"]: i for i in plan["items"]}


def _tree(path: Path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.exists() else []


def test_the_plan_lists_what_would_be_made_with_sizes_and_writes_nothing(env, source, tmp_path):
    before = _tree(tmp_path)
    plan = _plan(env, source)
    assert _tree(tmp_path) == before
    items = _by(plan)
    assert sorted(items) == ["persona:Andrea_Taini", "persona:Broken", "persona:Giulio_Cesare", "persona:Passages", "persona:Shan", "persona:Vanessa", "voice:sara"]
    shan = items["persona:Shan"]
    assert (shan["slug"], shan["action"], shan["conflict"]) == ("shan", "create", "")
    assert shan["parts"]["clips"]["count"] == 1 and shan["parts"]["memory"] == {"profile": True, "sessions": 2} and shan["parts"]["poster"] is True
    assert shan["skipped"] == [{"name": "wav2lip", "bytes": 2_000_000}]
    assert shan["personal"] is True and set(shan["personal_reasons"]) == {"memory", "video-or-photo"}
    assert "wiki-shan" not in json.dumps(shan) and any("knowledge project" in n for n in shan["notes"])
    assert items["persona:Andrea_Taini"]["personal"] is True
    assert items["persona:Vanessa"]["personal"] is False
    assert items["persona:Giulio_Cesare"]["parts"]["documents"]["count"] == 1 and items["persona:Giulio_Cesare"]["parts"]["passages"] == 1
    assert items["persona:Broken"]["action"] == "skip" and "manifest" in items["persona:Broken"]["problems"][0]
    assert items["voice:sara"]["personal"] is True
    assert PROFILE_1 not in json.dumps(plan) and SESSION not in json.dumps(plan)            # the plan shows counts, never the memory itself


def test_the_run_makes_the_personalities_and_nothing_personal_without_its_confirmation(env, source):
    out = _run(env, source, [{"id": "persona:Vanessa"}, {"id": "persona:Shan"}, {"id": "persona:Andrea_Taini"}, {"id": "voice:sara"}])
    status = {r["id"]: r["status"] for r in out["results"]}
    assert status == {"persona:Vanessa": "created", "persona:Shan": "needs_confirmation", "persona:Andrea_Taini": "needs_confirmation", "voice:sara": "needs_confirmation"}
    assert [p["slug"] for p in env.store.list()] == ["vanessa"]
    assert not env.avatars.exists() and not env.voices.exists()


def test_a_personality_gets_its_fields_its_encrypted_memory_its_archive_and_its_media(env, source):
    out = _run(env, source, [{"id": "persona:Shan", "confirm_personal": True}, {"id": "persona:Giulio_Cesare"}])
    got = {r["id"]: r for r in out["results"]}
    assert got["persona:Shan"]["status"] == "created" and got["persona:Giulio_Cesare"]["status"] == "created", out
    shan = env.store.get("shan")
    assert (shan["name"], shan["voice"], shan["personality"], shan["system_prompt"], shan["avatar"]) == ("Shan", {"engine": "xtts", "voice": "Chandra MacFarland"}, "Warm and a little ironic.", "You are Shan.", "shan")
    notes = env.store.memory("shan").items()
    texts = {n["text"] for n in notes}
    assert PROFILE_1 in texts and PROFILE_2 in texts and any("Spring trip" in t and SESSION in t and t.startswith("2025-") for t in texts)
    assert {n["kind"] for n in notes} == {"long", "short"} and {n["origin"] for n in notes} == {"inferred"}
    assert (env.avatars / "shan" / "clips" / "a.mp4").stat().st_size == 5000 + 12 and (env.avatars / "shan" / "poster.jpg").is_file()
    assert not (env.avatars / "shan" / "wav2lip").exists()                                  # the cache is left where it is
    caesar = env.store.get("giulio-cesare")
    assert caesar["profile"] == "A general who writes of his own wars." and len(caesar["knowledge"]) == 1
    archive = env.rag.get(caesar["knowledge"][0])
    assert archive["name"] == "Giulio_Cesare (callAPIcall)" and archive["documents"] == 1 and archive["chunks"] >= 1
    assert "avatar" not in {k for k in caesar if caesar[k] and k == "avatar"}


def test_the_memory_is_encrypted_on_disk_and_written_nowhere_else(env, source):
    _run(env, source, [{"id": "persona:Shan", "confirm_personal": True}])
    needles = [PROFILE_1.encode(), PROFILE_2.encode(), SESSION.encode(), b"Spring trip"]
    blob = (env.data / "personas" / "shan" / "memories.bin").read_bytes()
    assert blob[:4] == b"BPM1" and not any(n in blob for n in needles)
    for path in env.data.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert not any(n in data for n in needles), path


def test_without_documents_the_saved_passages_become_the_archive(env, source):
    out = _run(env, source, [{"id": "persona:Passages"}])
    assert out["results"][0]["status"] == "created"
    archive = env.rag.get(env.store.get("passages")["knowledge"][0])
    assert archive["documents"] == 1 and archive["document_list"][0]["name"] == "book.txt" and archive["chunks"] >= 1
    assert env.rag.search([archive["id"]], "cold winter north")["hits"]


def test_a_second_run_changes_nothing_and_a_persona_that_exists_is_skipped(env, source, tmp_path):
    items = [{"id": "persona:Shan", "confirm_personal": True}, {"id": "persona:Giulio_Cesare"}, {"id": "voice:sara", "confirm_personal": True}]
    first = _run(env, source, items)
    assert first["counts"] == {"created": 3}
    snapshot = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*") if p.is_file() and p.name != "device.key"}
    again = _run(env, source, items)
    assert again["counts"] == {"skipped": 3} and all("already exists" in r["message"] for r in again["results"])
    assert {p: p.read_bytes() for p in (tmp_path / "data").rglob("*") if p.is_file() and p.name != "device.key"} == snapshot


def test_replace_updates_the_personality_without_doubling_its_memory_archive_or_files(env, source):
    items = [{"id": "persona:Shan", "confirm_personal": True}, {"id": "persona:Giulio_Cesare"}]
    _run(env, source, items)
    memory = env.store.memory("shan")
    count, archives = memory.count(), len(env.rag.list())
    out = _run(env, source, [{**i, "replace": True} for i in items])
    assert out["counts"] == {"updated": 2}, out
    assert env.store.memory("shan").count() == count and len(env.rag.list()) == archives
    assert env.rag.get(env.store.get("giulio-cesare")["knowledge"][0])["documents"] == 1
    assert len(env.store.get("giulio-cesare")["knowledge"]) == 1


def test_a_persona_the_person_already_has_is_a_conflict_and_is_never_overwritten_by_default(env, source):
    env.store.create({"name": "Vanessa", "personality": "Mine, written by hand."})
    item = _by(_plan(env, source))["persona:Vanessa"]
    assert (item["conflict"], item["action"]) == ("exists", "skip")
    out = _run(env, source, [{"id": "persona:Vanessa"}])
    assert out["results"][0]["status"] == "skipped"
    assert env.store.get("vanessa")["personality"] == "Mine, written by hand."
    out = _run(env, source, [{"id": "persona:Vanessa", "replace": True}])
    assert out["results"][0]["status"] == "updated" and env.store.get("vanessa")["voice"] == {"engine": "kokoro", "voice": "if_sara"}


def test_a_broken_item_or_a_made_up_one_fails_alone_and_the_others_run(env, source):
    out = _run(env, source, [{"id": "persona:Broken"}, {"id": "persona:Nobody"}, {"id": "persona:Vanessa"}])
    assert [r["status"] for r in out["results"]] == ["failed", "failed", "created"]
    assert "manifest" in out["results"][0]["message"] and "no such item" in out["results"][1]["message"]


def test_a_cloned_voice_is_copied_into_the_models_folder_and_not_over_an_existing_one(env, source):
    assert _run(env, source, [{"id": "voice:sara", "confirm_personal": True}])["results"][0]["status"] == "created"
    assert (env.voices / "sara.wav").is_file()
    (env.voices / "sara.wav").write_bytes(b"mine")
    assert _run(env, source, [{"id": "voice:sara", "confirm_personal": True}])["results"][0]["status"] == "skipped"
    assert (env.voices / "sara.wav").read_bytes() == b"mine"
    assert _run(env, source, [{"id": "voice:sara", "confirm_personal": True, "replace": True}])["results"][0]["status"] == "updated"
    assert (env.voices / "sara.wav").read_bytes() != b"mine"


def test_the_run_needs_the_confirmation_and_a_real_folder(env, source, tmp_path):
    with pytest.raises(voice_import.Refused) as e:
        voice_import.run({"source": str(source), "items": [{"id": "persona:Vanessa"}]}, env.store, env.rag)
    assert e.value.status == 400 and "confirmation" in str(e.value)
    for bad in ("relative/path", str(tmp_path / "nowhere"), str(tmp_path), ""):
        with pytest.raises(voice_import.Refused):
            voice_import.plan({"source": bad}, env.store, env.rag)
    with pytest.raises(voice_import.Refused):
        voice_import.plan({}, env.store, env.rag)
    with pytest.raises(voice_import.Refused):
        voice_import.run({"source": str(source), "confirm": True, "items": []}, env.store, env.rag)


def test_the_data_folder_of_taDaMoo_is_not_a_source(env, tmp_path):
    inside = env.data / "callAPIcall" / "avatar" / "persone"
    inside.mkdir(parents=True)
    with pytest.raises(voice_import.Refused) as e:
        voice_import.plan({"source": str(env.data / "callAPIcall")}, env.store, env.rag)
    assert "data folder" in str(e.value)


def test_nothing_in_the_source_is_changed(env, source):
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in source.rglob("*") if p.is_file()}
    _run(env, source, [{"id": "persona:Shan", "confirm_personal": True}, {"id": "persona:Giulio_Cesare"}, {"id": "voice:sara", "confirm_personal": True}])
    assert {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in source.rglob("*") if p.is_file()} == before


def test_a_link_inside_the_source_is_not_followed(env, source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "manifest.json", {"name": "Linked"})
    try:
        os.symlink(outside, source / "avatar" / "persone" / "Linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symbolic links here")
    assert "persona:Linked" not in _by(_plan(env, source))


def test_the_routes_answer_this_pc_with_the_plan_and_the_run(env, source, monkeypatch):
    import http.client
    from serve import handler
    from serve.engine.mock import MockEngine
    from serve.frontend import ChatTemplate
    from serve.service import Service
    from serve.util.text import ByteTokenizer
    monkeypatch.setattr(handler, "_PERSONAS", None)
    monkeypatch.setattr(handler, "_RAG", None)
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, "ok", max_context=32768), tok, ChatTemplate(Path(handler.__file__).parent / "chat_template.jinja"))
    httpd = handler._listen(svc, "127.0.0.1", 0)

    def post(path, body):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=20)
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        r = conn.getresponse()
        out = json.loads(r.read())
        conn.close()
        return r.status, out
    try:
        code, plan = post("/api/voice/import/plan", {"source": str(source)})
        assert code == 200 and len(plan["items"]) == 7
        assert post("/api/voice/import/plan", {"source": "relative"})[0] == 400
        code, out = post("/api/voice/import/run", {"source": str(source), "items": [{"id": "persona:Vanessa"}]})
        assert code == 400 and out["error"]["type"] == "voice_import"
        code, out = post("/api/voice/import/run", {"source": str(source), "confirm": True, "items": [{"id": "persona:Vanessa"}, {"id": "persona:Shan"}]})
        assert code == 200 and out["counts"] == {"created": 1, "needs_confirmation": 1}
        assert (env.data / "personas" / "vanessa" / "persona.json").is_file()
    finally:
        httpd.shutdown()
        httpd.server_close()
