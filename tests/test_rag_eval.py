"""tools/rag_eval.py: the metrics, the language guess, finding a question's passage, and a whole run with the model-free stand-in embedder."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from tadamoo.rag import RagStore, project_key

spec = importlib.util.spec_from_file_location("rag_eval", Path(__file__).resolve().parents[1] / "tools" / "rag_eval.py")
rag_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rag_eval)

PROJ = project_key("project-one")
TEXTS = {
    "boats.txt": "The keel of a sailing boat keeps it upright and stops it sliding sideways in the wind.\n\nA sloop has one mast and two sails.",
    "rice.txt": "Risotto needs short grain rice, hot stock and patience. Stir often and add the stock slowly.",
    "cats.txt": "Cats sleep on the sofa all day and purr when they are stroked by their owners.",
}


def test_the_metrics_count_recall_at_k_and_the_reciprocal_rank():
    got = rag_eval.metrics([0, 2, 7, None, 60])
    assert got == {"n": 5, "R@1": 0.2, "R@5": 0.4, "R@10": 0.6, "MRR": round((1 + 1 / 3 + 1 / 8) / 5, 3)}
    assert rag_eval.metrics([])["n"] == 0


def test_a_rank_is_the_number_of_passages_ahead_and_a_floor_means_not_found():
    scores = np.array([0.5, 0.9, 0.5, 0.1])
    assert [rag_eval.rank_of(scores, i) for i in range(4)] == [1, 0, 2, 3]
    assert rag_eval.rank_of(np.array([0.0, 0.0, 2.0]), 0, 0.0) is None and rag_eval.rank_of(np.array([0.0, 3.0]), 1, 0.0) == 0


def test_the_language_of_a_passage_is_guessed_from_the_script_and_the_stop_words():
    assert rag_eval.guess_language("一种用于基因编辑的方法，包括以下步骤：提供细胞") == "zh"
    assert rag_eval.guess_language("The method of the invention is that the cell is treated with an agent for the purpose") == "en"
    assert rag_eval.guess_language("Le procédé de la présente invention est une méthode pour les cellules et des gènes dans le noyau") == "fr"
    assert rag_eval.guess_language("Il metodo della presente invenzione") == "other" and rag_eval.guess_language("") == "other"


def archive_with(tmp_path):
    s = RagStore(tmp_path / "rag")
    a = s.create("Mixed", project=PROJ)["id"]
    for name, text in TEXTS.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        s.add_file(a, name, p)
    return s, a


def test_a_question_names_its_passage_by_position_by_id_or_by_text(tmp_path):
    s, a = archive_with(tmp_path)
    arch = s.load(a)
    first = arch.chunks[0]
    assert rag_eval.resolve(arch, 0) == 0 and rag_eval.resolve(arch, 99) is None and rag_eval.resolve(arch, True) is None
    assert rag_eval.resolve(arch, f"{first['doc']}:{first['n']}") == 0 and rag_eval.resolve(arch, "d00000000:0") is None
    assert rag_eval.resolve(arch, "stock   and patience") == [i for i, c in enumerate(arch.chunks) if "Risotto" in c["text"]][0]
    assert rag_eval.resolve(arch, "") is None and rag_eval.resolve(arch, None) is None


def dump(store, aid):
    """Everything an archive holds, to see that a run left it as it was."""
    vectors = store._backend(aid).read_vectors(aid)
    return json.dumps([store.meta(aid), store.load(aid).chunks, store.load(aid).sections], sort_keys=True), b"" if vectors is None else vectors.tobytes()


def test_a_run_with_the_stand_in_embedder_reports_each_method_and_leaves_the_archive_alone(tmp_path, capsys):
    s, a = archive_with(tmp_path)
    before = dump(s, a)
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps({"questions": [
        {"passage": "keel of a sailing boat", "question": "what keeps the sailing boat upright with its keel"},
        {"passage": "short grain rice", "question": "how is risotto made with rice and hot stock"},
        {"passage": "sofa all day", "question": "where do cats sleep on the sofa"},
        {"passage": "text that is nowhere", "question": "anything at all"}]}), encoding="utf-8")
    out = tmp_path / "report.json"
    assert rag_eval.main(["--archive", a, "--questions", str(questions), "--rag-dir", str(tmp_path / "rag"), "--fake", "--json", str(out)]) == 0
    shown = capsys.readouterr().out
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["questions"] == 3 and len(report["skipped"]) == 1 and "1 skipped" in shown
    assert set(report["methods"]) == {"bm25", "dense", "fused", "dense+words", "search"}
    for name, row in report["methods"].items():
        assert row["all"]["R@1"] == 1.0 and row["all"]["MRR"] == 1.0, name
        assert "en" in row["by_language"] or "other" in row["by_language"]
    assert "R@10" in shown and "search" in shown
    assert dump(s, a) == before


def test_a_real_run_needs_vectors_of_the_model_in_use(tmp_path, monkeypatch):
    s, a = archive_with(tmp_path)
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps({"questions": []}), encoding="utf-8")
    monkeypatch.setattr(rag_eval, "real_embedder", lambda store, url: rag_eval.FakeEmbedder())
    with pytest.raises(SystemExit, match="index it again"):
        rag_eval.main(["--archive", a, "--questions", str(questions), "--rag-dir", str(tmp_path / "rag")])
