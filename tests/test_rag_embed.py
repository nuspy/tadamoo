"""Semantic search of the knowledge archives: the worker's command loop, the service that runs it on a lease of VRAM (with a stub worker that needs no
model), the store's vectors, and the retrieval pipeline (dense score plus a word share, threshold, rerank, duplicates)."""
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tadamoo.rag import RagError, RagStore, embed_worker, project_key
from tadamoo.rag import embed_client as ec
from tadamoo.rag.embed import embed_all

STUB = str(Path(__file__).with_name("rag_embed_stub.py"))
PROJ = project_key("project-one")
SAILING = ("Sailing boats use the wind. A sloop has one mast and two sails, the mainsail and the jib. To tack, the helmsman turns the bow through the wind.\n\n"
           "The keel keeps the boat upright and stops it sliding sideways. Racing yachts have deep keels and light hulls.")
COOKING = ("Risotto needs short grain rice, hot stock and patience. Stir often and add the stock a ladle at a time.\n\n"
           "Parmesan and butter are stirred in at the end, off the heat, to make it creamy.")


# -- the worker's command loop --------------------------------------------------------------------------

class Backend:
    def __init__(self):
        self.calls = []

    def load(self, role, model_dir, device):
        self.calls.append(("load", role, device))
        return device

    def unload(self, role):
        self.calls.append(("unload", role))

    def embed(self, texts, kind, instruction, batch, max_len, dim, progress):
        progress(len(texts), len(texts))
        return np.array([[len(t), 1.0, 3.0, 4.0] for t in texts], dtype=np.float32)

    def rerank(self, query, passages, instruction, batch, max_len, progress):
        return [0.25 * i for i, _ in enumerate(passages)]


def run_loop(commands, backend=None):
    sent = []
    code = embed_worker.serve(backend or Backend(), [json.dumps(c) + "\n" for c in commands], sent.append)
    return code, sent


def test_the_worker_answers_each_command_with_its_id_and_keeps_going_after_a_failure():
    code, sent = run_loop([{"id": 1, "cmd": "ping"}, {"id": 2, "cmd": "nope"}, {"id": 3, "cmd": "load", "role": "embed", "model_dir": "m", "device": "cpu"},
                           {"id": 4, "cmd": "rerank", "query": "q", "passages": ["a", "b", "c"]}, {"id": 5, "cmd": "quit"}, {"id": 6, "cmd": "ping"}])
    assert code == 0
    by = {m["id"]: m for m in sent if "id" in m}
    assert by[1]["ok"] and not by[2]["ok"] and "unknown command" in by[2]["error"] and by[3]["device"] == "cpu"
    assert by[4]["scores"] == [0.0, 0.25, 0.5] and by[5]["ok"] and 6 not in by                      # nothing runs after quit


def test_the_worker_returns_float16_vectors_cut_to_dim_and_normalized_again_inline_or_in_a_file(tmp_path):
    target = tmp_path / "v.npy"
    _, sent = run_loop([{"id": 1, "cmd": "embed", "texts": ["ab", "abcd"], "dim": 2}, {"id": 2, "cmd": "embed", "texts": ["ab"], "out": str(target)}])
    first = next(m for m in sent if m.get("id") == 1 and "ok" in m)
    assert first["dtype"] == "float16" and first["shape"] == [2, 2]
    import base64
    got = np.frombuffer(base64.b64decode(first["data"]), dtype=np.float16).reshape(2, 2).astype(np.float32)
    assert np.allclose(np.linalg.norm(got, axis=1), 1.0, atol=2e-3)
    second = next(m for m in sent if m.get("id") == 2 and "ok" in m)
    assert second["file"] == str(target) and "data" not in second and np.load(target).dtype == np.float16
    assert any(m.get("event") == "progress" and m["id"] == 1 for m in sent)                       # a long job says how far it is


def test_a_bad_line_is_answered_not_fatal():
    sent = []
    assert embed_worker.serve(Backend(), ["not json\n", "[1]\n", "\n"], sent.append) == 0
    assert [m["ok"] for m in sent] == [False, False]


# -- the service ----------------------------------------------------------------------------------------

class Lease:
    """A lease with no server: `policy(mib)` returns "" to grant it, else the refusal's reason."""
    made = []

    def __init__(self, owner, mib, purpose="", base_url=None, **kw):
        self.owner, self.mib, self.reason, self.message, self.granted_mib, self.released, self.lost = owner, mib, "", "", 0, False, False
        Lease.made.append(self)

    def acquire(self):
        why = Lease.policy(self.mib)
        if why:
            self.reason, self.message = why, "no: " + why
        else:
            self.granted_mib = self.mib
        return self

    @property
    def gpu_ok(self):
        return self.granted_mib > 0 and not self.lost and not self.released

    def release(self):
        self.released = True


@pytest.fixture(autouse=True)
def reset_leases():
    Lease.made, Lease.policy = [], staticmethod(lambda mib: "")
    yield


GIB4 = 7680 << 20                  # the weights of a 4B model in bf16, as the tiers see them


@pytest.fixture(autouse=True)
def weights(monkeypatch):
    monkeypatch.setattr(ec, "_weights", lambda folder: GIB4 if "4B" in folder.name else GIB4 // 6)


def models_folder(tmp_path, names=("Qwen3-Embedding-4B", "Qwen3-Reranker-4B", "Qwen3-Embedding-0.6B", "Qwen3-Reranker-0.6B")):
    root = tmp_path / "models"
    for n in names:
        d = root / "rag" / "embedding" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text("{}")
    return root


def service(tmp_path, settings=None, clock=time.monotonic, models=True, **kw):
    cfg = {"embed_enabled": True, "embed_python": sys.executable, "embed_model": "", "rerank": True, "rerank_model": "", "idle_s": 120, "embed_models_dir": ""}
    cfg.update(settings or {})
    root = models_folder(tmp_path) if models is True else models
    svc = ec.EmbedService(lambda: cfg, lambda: [str(root)] if root else [], lambda: "", argv=[sys.executable, "-X", "utf8", STUB], lease_factory=Lease, clock=clock, **kw)
    svc.spec_ttl = 0.0
    svc.cfg = cfg
    return svc


def log_of(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


@pytest.fixture
def stub_log(tmp_path, monkeypatch):
    path = tmp_path / "stub.log"
    monkeypatch.setenv("STUB_LOG", str(path))
    return path


def test_the_tiers_are_the_vram_to_ask_for_both_models_one_at_a_time_or_the_embedder_alone():
    assert ec.tiers(GIB4, GIB4) == {"both": 18000, "one": 13000, "embed": 9500}
    assert ec.tiers(1 << 30, 0)["embed"] == 3000


def test_the_models_are_found_by_name_and_chosen_by_size(tmp_path):
    root = models_folder(tmp_path)
    models = ec.model_folders([str(root / "rag" / "embedding")])
    assert sorted(m["name"] for m in models) == ["Qwen3-Embedding-0.6B", "Qwen3-Embedding-4B", "Qwen3-Reranker-0.6B", "Qwen3-Reranker-4B"]
    assert {m["name"]: m["kind"] for m in models}["Qwen3-Reranker-4B"] == "rerank"
    emb = ec.choose(models, "embed")
    assert emb["name"] == "Qwen3-Embedding-4B" and ec.choose(models, "rerank", like=emb["name"])["name"] == "Qwen3-Reranker-4B"
    assert ec.choose(models, "embed", "Qwen3-Embedding-0.6B")["name"] == "Qwen3-Embedding-0.6B" and ec.choose(models, "embed", "nope") is None
    assert ec.model_folders([str(tmp_path / "missing")]) == []


def test_no_python_no_model_or_a_switch_off_means_no_embedder_and_says_why(tmp_path):
    svc = service(tmp_path, {"embed_enabled": False})
    assert svc.provider() is None and svc.info()["reason"] == "off" and svc.info()["available"] is False
    svc = service(tmp_path, {"embed_python": ""}, venv_roots=(), pythons=lambda: [], probe_fn=lambda p: "")
    assert svc.provider() is None and svc.info()["reason"] in ("detecting", "no_python")
    for _ in range(100):                                                                           # the look for a Python runs in the background
        if svc._detected is not None:
            break
        time.sleep(0.05)
    assert svc.provider() is None and svc.info()["reason"] == "no_python"
    svc = service(tmp_path, {"embed_python": str(tmp_path / "nothing.exe")})
    assert svc.provider() is None and svc.info()["reason"] == "no_python"
    svc = service(tmp_path, models=tmp_path / "empty")
    assert svc.provider() is None and svc.info()["reason"] == "no_model"


def test_a_system_python_with_torch_is_found_once_and_one_with_cuda_comes_first(tmp_path):
    seen = []

    def probe(python):
        seen.append(python)
        return {"a": "cpu", "b": "cuda", "c": "cuda"}[python]
    assert ec.detect(lambda: ["a", "b", "c"], probe) == "b" and seen == ["a", "b"]
    assert ec.detect(lambda: ["a"], probe) == "a" and ec.detect(lambda: [], probe) == ""
    svc = service(tmp_path, {"embed_python": ""}, venv_roots=(), pythons=lambda: [sys.executable], probe_fn=lambda p: "cuda")
    svc.provider()
    for _ in range(100):
        if svc._detected is not None:
            break
        time.sleep(0.05)
    assert svc.provider() is not None and svc.info()["python"] == sys.executable


def test_a_lease_for_both_models_puts_everything_on_the_gpu(tmp_path, stub_log):
    svc = service(tmp_path)
    try:
        emb = svc.provider()
        assert emb.model == "Qwen3-Embedding-4B" and emb.version == ec.VERSION
        v = emb.embed(["The yacht has a tall mast", "risotto with butter"])
        assert v.shape[0] == 2 and v.dtype == np.float32 and np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-2)
        q = emb.embed_query("a boat mast")
        assert q.shape == (v.shape[1],) and float(v[0] @ q) > float(v[1] @ q)
        scores = emb.rerank("keel boat", ["the keel of a boat", "rice", "boat"])
        assert scores is not None and scores[0] > scores[1]
        assert [(l.owner, l.mib) for l in Lease.made] == [("embedding", 18000)]
        info = svc.info()
        assert info["device"] == "cuda" and info["mode"] == "both" and info["lease_mib"] == Lease.made[0].mib
        log = log_of(stub_log)
        assert [(r["role"], r["device"]) for r in log if r["cmd"] == "load"] == [("embed", "cuda"), ("rerank", "cuda")]      # both resident, loaded on demand
        assert [r["kind"] for r in log if r["cmd"] == "embed"] == ["passage", "query"]
        assert [r["instruction"] for r in log if r["cmd"] == "embed"][1].startswith(ec.INSTRUCTION[:20])
        assert len(Lease.made) == 1                                                                 # one lease serves every request
    finally:
        svc.close()
    assert Lease.made[0].released


def test_without_a_lease_a_question_is_embedded_on_the_cpu_and_nothing_is_reranked(tmp_path, stub_log):
    Lease.policy = staticmethod(lambda mib: "busy")
    svc = service(tmp_path)
    try:
        emb = svc.provider()
        q = emb.embed_query("a boat mast")
        assert q.shape[0] == 4 and svc.info()["device"] == "cpu" and svc.info()["lease_reason"] == "busy"
        assert emb.rerank("keel", ["keel", "rice"]) is None
        assert [(r["role"], r["device"]) for r in log_of(stub_log) if r["cmd"] == "load"] == [("embed", "cpu")]
        assert len(Lease.made) == 1 and emb.batch == ec.CPU_BATCH                                   # a busy GPU is not asked again at every question
        emb.embed_query("again")
        assert len(Lease.made) == 1
    finally:
        svc.close()


def test_the_gpu_is_asked_again_after_a_while_and_the_worker_moves_to_it(tmp_path, stub_log):
    now = {"t": 1000.0}
    Lease.policy = staticmethod(lambda mib: "busy")
    svc = service(tmp_path, clock=lambda: now["t"])
    try:
        emb = svc.provider()
        emb.embed_query("a boat")
        assert svc.info()["device"] == "cpu"
        Lease.policy = staticmethod(lambda mib: "")
        now["t"] += ec.RETRY_S + 1
        emb.embed_query("a boat again")
        assert svc.info()["device"] == "cuda" and [r["device"] for r in log_of(stub_log) if r["cmd"] == "load"] == ["cpu", "cuda"]
    finally:
        svc.close()


def test_when_both_do_not_fit_one_model_is_loaded_at_a_time(tmp_path, stub_log):
    Lease.policy = staticmethod(lambda mib: "not_enough" if mib > 13000 else "")                   # only the two-model ask is refused
    svc = service(tmp_path)
    try:
        emb = svc.provider()
        emb.embed_query("a boat")
        assert svc.info()["mode"] == "one" and [l.mib for l in Lease.made] == [18000, 13000] and Lease.made[0].granted_mib == 0
        assert emb.rerank("boat", ["boat", "rice"]) is not None
        emb.embed_query("a boat")
        cmds = [(r["cmd"], r.get("role")) for r in log_of(stub_log) if r["cmd"] in ("load", "unload")]
        assert cmds == [("load", "embed"), ("unload", "embed"), ("load", "rerank"), ("unload", "rerank"), ("load", "embed")]
    finally:
        svc.close()


def test_a_lease_that_is_not_enough_for_the_reranker_still_embeds_and_skips_the_rerank(tmp_path, stub_log):
    Lease.policy = staticmethod(lambda mib: "not_enough" if mib > 9500 else "")
    svc = service(tmp_path)
    try:
        emb = svc.provider()
        assert emb.embed_query("a boat").shape[0] == 4
        assert svc.info()["device"] == "cuda" and svc.info()["mode"] == "embed" and [l.mib for l in Lease.made] == [18000, 13000, 9500]
        assert emb.rerank("boat", ["boat"]) is None
    finally:
        svc.close()


def test_a_python_without_cuda_gives_the_lease_back_and_runs_on_the_cpu(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_NO_CUDA", "1")
    svc = service(tmp_path)
    try:
        svc.provider().embed_query("a boat")
        assert svc.info()["device"] == "cpu" and Lease.made[0].released and svc.info()["lease_reason"] == "no_cuda"
    finally:
        svc.close()


def test_the_worker_and_the_lease_are_given_back_after_the_idle_time_and_come_back_on_demand(tmp_path, stub_log):
    svc = service(tmp_path, {"idle_s": 0.3})
    try:
        emb = svc.provider()
        emb.embed_query("a boat")
        assert svc._worker is not None
        for _ in range(100):
            if svc._worker is None:
                break
            time.sleep(0.05)
        assert svc._worker is None and Lease.made[0].released and svc.info()["device"] == ""
        emb.embed_query("a boat again")                                                          # the next question starts them again
        assert svc._worker is not None and len(Lease.made) == 2
    finally:
        svc.close()


def test_resting_gives_the_worker_and_the_lease_back_at_once_but_not_during_a_request(tmp_path, stub_log):
    svc = service(tmp_path)
    try:
        emb = svc.provider()
        emb.embed_query("a boat")
        assert svc._worker is not None
        svc._busy = 1
        emb.rest()
        assert svc._worker is not None and not Lease.made[0].released
        svc._busy = 0
        emb.rest()
        assert svc._worker is None and Lease.made[0].released
    finally:
        svc.close()


def test_a_worker_that_dies_is_started_again_for_the_next_request(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_DIE_AFTER", "1")
    svc = service(tmp_path)
    try:
        emb = svc.provider()
        emb.embed(["boat"])
        with pytest.raises(RagError):
            emb.embed(["boat"])
        monkeypatch.delenv("STUB_DIE_AFTER")
        assert emb.embed(["boat"]).shape[0] == 1 and len(Lease.made) == 2 and Lease.made[0].released
    finally:
        svc.close()


def test_a_lost_lease_restarts_the_worker(tmp_path):
    svc = service(tmp_path)
    try:
        emb = svc.provider()
        emb.embed(["boat"])
        Lease.made[0].lost = True
        emb.embed(["boat"])
        assert len(Lease.made) == 2 and Lease.made[0].released
    finally:
        svc.close()


def test_a_big_batch_comes_back_in_a_file_and_the_file_is_removed(tmp_path):
    svc = service(tmp_path, tmp_dir=str(tmp_path))
    try:
        got = svc.embed(["boat %d" % i for i in range(ec.FILE_ABOVE + 5)], "passage")
        assert got.shape[0] == ec.FILE_ABOVE + 5 and not list(tmp_path.glob("*.npy"))
    finally:
        svc.close()


def test_indexing_tells_its_progress_and_its_device_and_can_be_cancelled(tmp_path, monkeypatch):
    Lease.policy = staticmethod(lambda mib: "busy")
    svc = service(tmp_path)
    events, cancel = [], threading.Event()

    def progress(event):
        events.append(event)
        if event["page"] >= 4:
            cancel.set()
    try:
        emb = svc.provider()
        with pytest.raises(RagError) as e:
            embed_all(emb, ["boat %d" % i for i in range(40)], progress, cancel)
        assert e.value.code == "cancelled"
        assert events[0]["pages"] == 40 and events[-1]["device"] == "cpu" and events[-1]["page"] < 40           # the slow device asks for small batches
    finally:
        svc.close()


def test_the_test_button_embeds_a_sentence_and_says_where(tmp_path):
    svc = service(tmp_path)
    try:
        out = svc.test()
        assert out["ok"] and out["device"] == "cuda" and out["dim"] == 4 and out["model"] == "Qwen3-Embedding-4B"
    finally:
        svc.close()
    off = service(tmp_path, {"embed_enabled": False})
    assert off.test()["ok"] is False


# -- the store with the service -------------------------------------------------------------------------

def write(tmp_path, name, content):
    p = tmp_path / "src" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_documents_are_embedded_by_the_worker_and_the_meta_keeps_model_size_and_version(tmp_path):
    svc = service(tmp_path)
    try:
        s = RagStore(tmp_path / "rag", embedder=svc.provider)
        a = s.create("Boats", project=PROJ)["id"]
        events = []
        out = s.add_file(a, "boats.txt", write(tmp_path, "boats.txt", SAILING), events.append)
        assert out["embedded"] is True
        meta = s.load(a).meta
        assert meta["embed_model"] == "Qwen3-Embedding-4B" and meta["embed_dim"] == 4 and meta["embed_version"] == ec.VERSION
        assert any(e.get("event") == "progress" and e["device"] == "cuda" for e in events)
        assert s._backend(a).read_vectors(a).dtype == np.float32 and s._backend(a).vector_note(a)["dims"] == 4
        found = s.search([a], "yacht keel")
        assert found["mode"] == "hybrid" and found["hits"][0]["stages"][:2] == ["lexical", "dense"] and found["reranked"] is True
    finally:
        svc.close()


def test_changing_the_model_marks_archives_to_index_again_and_vectors_of_two_models_never_mix(tmp_path):
    svc = service(tmp_path, {"embed_model": "Qwen3-Embedding-4B"})
    try:
        s = RagStore(tmp_path / "rag", embedder=svc.provider)
        a = s.create("Boats", project=PROJ)["id"]
        s.add_file(a, "boats.txt", write(tmp_path, "boats.txt", SAILING))
        assert s.get(a)["mode"] == "hybrid" and s.get(a)["reindex"] is False
        svc.cfg["embed_model"] = "Qwen3-Embedding-0.6B"
        assert s.get(a)["mode"] == "text" and s.get(a)["reindex"] is True
        assert s.search([a], "keel")["mode"] == "lexical"                                          # its vectors are of another model: words only
        out = s.add_file(a, "rice.txt", write(tmp_path, "rice.txt", COOKING))
        assert out["embedded"] is False and "index it again" in out["note"] and s.load(a).meta["embed_model"] == ""
        done = s.reindex(a)
        assert done["embedded"] is True and s.load(a).meta["embed_model"] == "Qwen3-Embedding-0.6B" and s.get(a)["reindex"] is False
        svc.cfg["embed_enabled"] = False
        assert s.get(a)["mode"] == "text" and s.get(a)["reindex"] is False                         # off is not a reason to index again
    finally:
        svc.close()


def test_another_version_of_the_vectors_means_indexing_again(tmp_path, monkeypatch):
    svc = service(tmp_path)
    try:
        s = RagStore(tmp_path / "rag", embedder=svc.provider)
        a = s.create("Boats", project=PROJ)["id"]
        s.add_file(a, "boats.txt", write(tmp_path, "boats.txt", SAILING))
        monkeypatch.setattr(ec.ServiceEmbedder, "version", "q3e-2")
        assert s.get(a)["mode"] == "text" and s.get(a)["reindex"] is True
    finally:
        svc.close()


def test_a_reindex_leaves_the_archive_searchable_and_can_be_cancelled(tmp_path):
    svc = service(tmp_path)
    try:
        s = RagStore(tmp_path / "rag", embedder=svc.provider)
        a = s.create("Mixed", project=PROJ)["id"]
        s.add_file(a, "boats.txt", write(tmp_path, "boats.txt", SAILING))
        before = s.load(a).meta["updated"]
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(RagError) as e:
            s.reindex(a, cancel=cancel)
        assert e.value.code == "cancelled" and s.load(a).meta["updated"] == before and s.search([a], "keel")["hits"]
    finally:
        svc.close()


def test_a_document_cancelled_while_it_is_indexed_leaves_the_archive_as_it_was(tmp_path):
    svc = service(tmp_path)
    cancel = threading.Event()
    try:
        s = RagStore(tmp_path / "rag", embedder=svc.provider)
        a = s.create("Boats", project=PROJ)["id"]
        with pytest.raises(RagError) as e:
            s.add_file(a, "boats.txt", write(tmp_path, "boats.txt", SAILING), lambda ev: cancel.set() if ev.get("stage") == "indexing" else None, cancel)
        assert e.value.code == "cancelled" and s.get(a)["documents"] == 0 and not list((tmp_path / "rag" / a / "blocks").glob("*"))
    finally:
        svc.close()


def test_the_harness_asks_the_server_and_falls_back_to_words_when_it_does_not_answer(tmp_path):
    s = RagStore(tmp_path / "rag")
    a = s.create("Boats", project=PROJ)["id"]
    s.add_file(a, "boats.txt", write(tmp_path, "boats.txt", SAILING))
    asked = []

    def server(ids, query, top_k, min_share):
        asked.append((ids, query, top_k, min_share))
        return {"mode": "hybrid", "hits": [{"text": "from the server"}], "note": "", "stages": []}
    s.remote = server
    assert s.search([a], "keel", top_k=3, min_share=0.25)["hits"] == [{"text": "from the server"}] and asked == [([a], "keel", 3, 0.25)]

    def down(*args):
        raise OSError("refused")
    s.remote = down
    assert s.search([a], "keel")["mode"] == "lexical"
    from tadamoo.rag.remote import server_search
    assert server_search("http://10.0.0.5:7242") is None and server_search("http://127.0.0.1:7242") is not None and server_search("") is None


# -- the retrieval pipeline -----------------------------------------------------------------------------

class Topics:
    """An in-process embedder with the stub's meaning, with a reranker that can be steered."""
    model, version, device = "topics", "v1", "cuda"
    batch = 32

    def __init__(self, rerank=None, fail=False):
        self.scores, self.fail, self.asked = rerank, fail, []

    def embed(self, texts):
        from rag_embed_stub import vector
        return np.vstack([vector(t) for t in texts])

    def rerank(self, query, passages):
        self.asked.append(list(passages))
        if self.fail:
            raise RuntimeError("out of memory")
        return None if self.scores is None else self.scores(query, passages)


def pipeline(tmp_path, texts, emb, **cfg):
    """A store with one archive holding each of `texts` as a document, and the settings `cfg`."""
    s = RagStore(tmp_path / "rag", embedder=lambda: s.holder["e"])
    s.holder = {"e": emb}
    s.settings.write(cfg)
    aid = s.create("Boats", project=PROJ)["id"]
    for i, text in enumerate(texts):
        s.add_file(aid, f"d{i}.txt", write(tmp_path, f"d{i}.txt", text))
    return s, (aid,)


PASSAGES = ["The keel of the boat keeps it upright. A boat needs a keel.", "Risotto needs rice and stock and butter.", "Cats sleep on the divano all day long, cat after cat."]


def test_the_score_is_the_cosine_plus_a_small_share_of_the_word_score(tmp_path):
    from tadamoo.rag import search as ranking
    s, (a,) = pipeline(tmp_path, PASSAGES, Topics(), rerank=False)
    arch = s.load(a)
    q = Topics().embed(["keel boat"])[0]
    q = q / np.linalg.norm(q)
    out = s.search([a], "keel boat")
    top = out["hits"][0]
    assert top["document"] == "d0.txt" and out["mode"] == "hybrid" and out["reranked"] is False and top["stages"] == ["lexical", "dense"]
    lex = ranking.bm25([arch.index()], "keel boat")[0]
    cos = float(arch.vectors[0] @ q)
    want = cos + 0.05 * lex[0] / max(lex.values())
    assert abs(top["score"] - round(want, 4)) < 2e-4 and abs(top["semantic"] - round(cos, 4)) < 2e-4 and top["lexical"] == round(lex[0], 4)
    s.settings.write({"lexical_weight": 0})
    assert abs(s.search([a], "keel boat")["hits"][0]["score"] - round(cos, 4)) < 2e-4


def test_the_words_break_a_tie_but_do_not_beat_the_meaning(tmp_path):
    twins = ["A yacht sails with a mast and a keel.", "A yacht sails with a mast and a keel, say the sailors.", "Risotto with rice and butter."]
    s, (a,) = pipeline(tmp_path, twins, Topics(), rerank=False, min_cosine=0.0)
    hits = s.search([a], "sailors", top_k=3)["hits"]
    assert hits[0]["document"] == "d1.txt" and hits[0]["lexical"] and hits[1]["lexical"] is None            # the only passage with the word wins the tie


def test_a_passage_too_far_from_the_question_is_not_given_and_the_threshold_is_a_setting(tmp_path):
    s, (a,) = pipeline(tmp_path, PASSAGES, Topics(), rerank=False)
    assert [h["document"] for h in s.search([a], "keel boat")["hits"]] == ["d0.txt"]                           # the others are orthogonal to the question
    s.settings.write({"min_cosine": 0.0})
    assert len(s.search([a], "keel boat")["hits"]) == 3
    s.settings.write({"min_cosine": 0.9})
    assert s.search([a], "keel boat")["hits"] == [] or s.search([a], "keel boat")["hits"][0]["semantic"] >= 0.9


def test_the_share_of_words_asked_of_a_words_only_answer_does_not_drop_a_vector_hit(tmp_path):
    s, (a,) = pipeline(tmp_path, PASSAGES, Topics(), rerank=False)
    out = s.search([a], "mast sail", min_share=0.9)                                                               # no word in common with any passage
    assert out["hits"] and out["hits"][0]["semantic"] is not None
    s.holder["e"] = None
    assert s.search([a], "mast sail", min_share=0.9)["hits"] == []


def test_the_reranker_reorders_the_best_twenty_and_a_low_score_drops_a_passage(tmp_path):
    texts = ["The keel of the boat keeps it upright.", "A boat needs a keel and a mast and a sail, a boat keel for the sea.", "The mast of a yacht is tall."]
    emb = Topics(rerank=lambda q, ps: [0.3 if "needs" in p else 0.8 if "upright" in p else 0.1 for p in ps])
    s, (a,) = pipeline(tmp_path, texts, emb, min_cosine=0.0, min_rerank=0.2)
    out = s.search([a], "boat keel mast", top_k=3)
    assert out["reranked"] is True and out["stages"] == ["lexical", "dense", "rerank"]
    assert [h["document"] for h in out["hits"]] == ["d0.txt", "d1.txt"] and out["hits"][0]["rerank"] == 0.8 and out["hits"][0]["score"] == 0.8     # the third scored 0.1: dropped
    assert len(emb.asked) == 1 and len(emb.asked[0]) == 3 and all("keel" in p or "mast" in p for p in emb.asked[0])
    s.settings.write({"rerank": False})
    assert s.search([a], "boat keel mast")["reranked"] is False and len(emb.asked) == 1


def test_no_reranker_available_or_failing_keeps_the_vector_order_and_says_so(tmp_path):
    s, (a,) = pipeline(tmp_path, PASSAGES, Topics(rerank=None))
    out = s.search([a], "keel boat")
    assert out["reranked"] is False and out["hits"][0]["document"] == "d0.txt" and "not reranked" in out["note"]
    s, (a,) = pipeline(tmp_path / "x", PASSAGES, Topics(fail=True))
    out = s.search([a], "keel boat")
    assert out["hits"] and out["reranked"] is False and "reranker failed" in out["note"]


def test_identical_passages_count_once(tmp_path):
    s, (a,) = pipeline(tmp_path, ["The keel of the boat.", "The  KEEL of the boat.", "A different boat keel"], Topics(), rerank=False, min_cosine=0.0)
    hits = s.search([a], "boat keel", top_k=5)["hits"]
    assert len(hits) == 2 and len({h["text"].lower().replace("  ", " ") for h in hits}) == 2


def test_an_archive_without_vectors_is_searched_by_words_after_the_others_with_a_note(tmp_path):
    s = RagStore(tmp_path / "rag", embedder=lambda: None)
    a, b = s.create("Words", project=PROJ)["id"], s.create("Vectors", project=PROJ)["id"]
    s.add_file(a, "w.txt", write(tmp_path, "w.txt", "The keel of a boat, written long ago."))
    s.provider = lambda: Topics()
    s.add_file(b, "v.txt", write(tmp_path, "v.txt", "A yacht has a mast. The boat keel and the mast."))
    out = s.search([a, b], "boat keel", top_k=3)
    assert [h["archive"] for h in out["hits"]] == ["Vectors", "Words"] and out["hits"][1]["stages"] == ["lexical"] and "index them again" in out["note"]


def test_a_failing_embedder_at_question_time_means_words_only(tmp_path):
    emb = Topics()
    s, (a,) = pipeline(tmp_path, PASSAGES, emb)

    def boom(texts):
        raise RuntimeError("worker ended")
    emb.embed = boom
    out = s.search([a], "keel boat")
    assert out["mode"] == "lexical" and "embedder failed" in out["note"] and out["hits"]


def test_the_settings_are_checked_and_kept(tmp_path):
    s = RagStore(tmp_path / "rag")
    got = s.settings.write({"lexical_weight": 0.1, "idle_s": 30, "rerank": False, "embed_model": "Qwen3-Embedding-4B", "min_cosine": 0.4, "min_rerank": 0.3})
    assert got["lexical_weight"] == 0.1 and got["idle_s"] == 30 and got["rerank"] is False and RagStore(tmp_path / "rag").settings.read()["embed_model"] == "Qwen3-Embedding-4B"
    for bad in ({"lexical_weight": 0.5}, {"lexical_weight": "x"}, {"idle_s": 5}, {"rerank": "yes"}, {"embed_model": "a/b"}, {"embed_python": str(tmp_path / "no.exe")},
                {"embed_models_dir": str(tmp_path / "nowhere")}, {"min_cosine": True}, {"colour": 1}):
        with pytest.raises(RagError):
            s.settings.write(bad)
    assert s.settings.read()["lexical_weight"] == 0.1
    assert RagStore(tmp_path / "fresh").settings.read() == {**RagStore(tmp_path / "fresh").settings.read(), "lexical_weight": 0.05, "idle_s": 120, "embed_enabled": True, "rerank": True}
