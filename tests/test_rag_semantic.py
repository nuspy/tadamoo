"""Cutting where the meaning changes, the size settings of the structural cut, and the tool that compares ways of cutting."""
import importlib.util
import json
from pathlib import Path

import numpy as np

from tadamoo.rag import RagStore, project_key
from tadamoo.rag.ingest import chunker, semantic

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rag_eval, chunk_eval = load_tool("rag_eval"), load_tool("rag_chunk_eval")


def topics(texts):
    """Two topics, cats and dogs, and a third for the rest: unit-length rows."""
    rows = np.array([[1.0 if "cat" in t else 0.0, 1.0 if "dog" in t else 0.0, 0.2] for t in texts], dtype=np.float32)
    return rows / np.linalg.norm(rows, axis=1, keepdims=True)


def para(word, n=8):
    return {"type": "paragraph", "text": (f"The {word} sat on the mat and the {word} looked at the {word}. ") * n, "page": 1}


def test_a_valley_in_the_similarity_of_the_units_is_found_and_a_flat_run_has_none():
    vectors = topics(["cat"] * 5 + ["dog"] * 5)
    assert semantic.valleys_of(vectors, window=2, sensitivity=0.5) == [5]
    assert semantic.valleys_of(topics(["cat"] * 6), 2, 0.5) == [] and semantic.valleys_of(topics(["cat", "dog"]), 2, 0.5) == []


def test_the_passages_follow_the_topics_with_no_size_target_and_small_neighbours_join_only_when_close():
    blocks = [{"type": "heading", "text": "Pets", "level": 1, "page": 1}] + [para("cat", 20) for _ in range(4)] + [para("dog", 20) for _ in range(4)]
    out = semantic.semantic_chunks(blocks, "doc", topics, soft_min=50)
    assert len(out["chunks"]) == 2 and out["chunks"][0]["text"].count("cat") > 0 and "dog" not in out["chunks"][0]["text"] and "cat" not in out["chunks"][1]["text"]
    assert out["chunks"][0]["path"] == "doc > Pets" and chunker.tokens(out["chunks"][0]["text"]) > chunker.HARD                    # longer than the structural limit: the topic is the unit
    small = [{"type": "heading", "text": "A", "level": 1, "page": 1}, para("cat", 1), para("dog", 1), para("cat", 1)]
    assert len(semantic.semantic_chunks(small, "doc", topics, merge_tau=0.0)["chunks"]) == 1                                     # small and close enough (here: always): one
    assert len(semantic.semantic_chunks(small, "doc", topics, merge_tau=2.0, soft_min=10)["chunks"]) >= 1


def test_a_table_stays_whole_and_a_passage_is_cut_by_force_only_at_the_safety_maximum():
    table = {"type": "table", "text": "| a | b |\n| --- | --- |\n" + "\n".join(f"| row {i} cat | {i} |" for i in range(200)), "page": 2}
    blocks = [{"type": "heading", "text": "T", "level": 1, "page": 1}, para("cat", 2), table, para("cat", 2)]
    out = semantic.semantic_chunks(blocks, "doc", topics)
    kinds = [c["kind"] for c in out["chunks"]]
    assert kinds.count("table") == 1 and chunker.tokens([c for c in out["chunks"] if c["kind"] == "table"][0]["text"]) > chunker.HARD
    cut = semantic.semantic_chunks([{"type": "heading", "text": "T", "level": 1, "page": 1}] + [para("cat") for _ in range(30)], "doc", topics, max_tokens=500, soft_min=10)
    assert len(cut["chunks"]) > 2 and all(chunker.tokens(c["text"]) <= 560 for c in cut["chunks"])
    big = semantic.semantic_chunks([{"type": "heading", "text": "T", "level": 1, "page": 1}, table], "doc", topics, table_max=300)
    assert len(big["chunks"]) > 1 and all(c["text"].startswith("| a | b |") for c in big["chunks"])                              # cut by rows only beyond the limit, header repeated


def test_the_seam_of_the_structural_cut_takes_the_sentence_valleys_and_the_sizes_are_arguments():
    sentences = ["The cat sat there. "] * 12 + ["A dog ran there. "] * 12
    text = "".join(sentences).strip()
    by_size = chunker.split_prose(text, target=60, hard=120)
    by_valley = chunker.split_prose(text, target=60, hard=120, valleys=semantic.sentence_valleys(topics))
    assert any(p.startswith("A dog") for p in by_valley) and len(by_valley) >= 2 and len(by_size) >= 2
    blocks = [{"type": "heading", "text": "H", "level": 1, "page": 1}] + [para("cat", 3) for _ in range(12)]
    small = chunker.chunk_blocks(blocks, "d", target=100, hard=160)["chunks"]
    default = chunker.chunk_blocks(blocks, "d")["chunks"]
    assert len(small) > len(default) and all(chunker.tokens(c["text"]) <= 170 for c in small)
    assert chunker.chunk_blocks(blocks, "d", target=chunker.TARGET, hard=chunker.HARD)["chunks"] == default


# -- the comparison tool --------------------------------------------------------------------------------------------------------------

def test_a_passage_holds_the_evidence_by_its_text_whatever_the_cut_and_one_that_shares_common_words_does_not():
    evidence = {"windows": chunk_eval.windows("The vector was injected into the vitreous of the eye of each mouse at a dose of two milligrams per kilogram of body weight.")}
    assert chunk_eval.coverage(evidence, "Earlier text. The vector was injected into the vitreous of the eye of each mouse at a dose of two milligrams per kilogram of body weight. Later.") == 1.0
    assert chunk_eval.coverage(evidence, "The vector was injected into the vitreous of the eye of each mouse") < chunk_eval.HOLD
    assert chunk_eval.coverage(evidence, "the of the and the of a dose of the eye of the mouse") < chunk_eval.PART
    flat = chunk_eval.flat_chunks("\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10)), size=400, overlap=50)
    assert len(flat) > 3 and all(len(c) <= 500 for c in flat)


def test_the_variants_are_built_and_judged_and_the_table_reads(tmp_path):
    store = RagStore(tmp_path / "rag")
    aid = store.create("Pets", project=project_key("p"))["id"]
    store.add_bytes(aid, "pets.md", ("# Cats\n\n" + "\n\n".join(f"The cat number {i} sat on the soft mat and purred while the dog slept. " * 3 for i in range(6))
                                      + "\n\n# Dogs\n\n" + "\n\n".join(f"The dog number {i} ran in the park and barked at every bird it saw. " * 3 for i in range(6))).encode())
    chunks = store.load(aid).chunks
    questions = [{"passage": f"{c['doc']}:{c['n']}", "question": c["text"][:60]} for c in chunks[:3]]
    evidence = chunk_eval.evidence_of(store, aid, questions)
    assert len(evidence) == 3 and evidence[0]["lang"] in ("en", "other")
    names, blocks = chunk_eval.load_blocks(store, aid)
    memory = chunk_eval.Memory(rag_eval.FakeEmbedder(), tmp_path / "cache")
    report = {}
    for name in ("flat", "struct-150-250", "struct-350-600", "sem-free", "sem-valleys", "struct-merge", "parent-nbr", "parent-section"):
        variant = chunk_eval.build(name, names, blocks, memory)
        assert variant["chunks"] and (name != "parent-nbr" or variant["parents"])
        report[name] = chunk_eval.evaluate(variant, names, memory, evidence, rag_eval.RETRIEVAL, False)
    assert report["struct-350-600"]["all"]["R@10"] == 1.0 and report["struct-350-600"]["split"] == 0
    assert report["struct-150-250"]["passages"] > report["struct-350-600"]["passages"]
    again = chunk_eval.Memory(rag_eval.FakeEmbedder(), tmp_path / "cache")                         # the vectors were kept: nothing is embedded twice
    again.embed([c["text"] for c in chunks[:2]])
    assert again.stats["texts"] == 0 or again.stats["texts"] <= 2
    text = chunk_eval.table(report)
    assert "struct-350-600" in text and "R@5" in text and json.dumps(report)
