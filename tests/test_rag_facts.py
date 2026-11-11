"""What is read off passages and documents by rule (no model): numbers, sequences, dates, symbols, quantities, language, the facts of a front page, and the filters
that use them in a search."""
import pytest

from tadamoo.rag import RagError, RagStore, lookup, project_key
from tadamoo.rag.ingest import meta
from tadamoo.rag.recall import ProjectRecall

PROJ = project_key("project-facts")
FRONT = ("(10) United States Patent (12) Sinclair et al.\n\n(54) METHODS FOR TREATING GLAUCOMA\n\n(71) Applicant: President and Fellows of Harvard College, Cambridge, MA (US)\n\n"
         "(72) Inventors: David A. Sinclair, Cambridge, MA (US)\n\n(73) Assignee: Harvard College\n\n(43) Pub. Date: 12 October 2023\n")


def test_patent_and_publication_numbers_are_found_in_their_common_forms_and_a_pct_number_is_not_also_a_national_one():
    got = meta.patent_numbers("See US12274733B2, WO 2023/196851 A1 and CN 118374548 A, filed as PCT/US2023/071689 and EP 23382042.2.")
    assert got[0] == "PCT/US2023/071689" and {"US12274733B2", "WO2023196851A1", "CN118374548A", "EP23382042.2"} <= set(got)
    assert not any(n.startswith("US2023071689") for n in got) and meta.patent_numbers("room 2023 and AU018091") == []


def test_sequence_identifiers_come_from_lists_and_ranges():
    assert meta.sequence_ids("OCT4 is SEQ ID NO: 41, SOX2 SEQ ID NO:43 and KLF4 SEQ ID NO. 45; SEQ ID NOs: 1-3 and 10") == [1, 2, 3, 10, 41, 43, 45]
    assert meta.sequence_ids("no identifiers here, 41 and 43") == []


def test_dates_are_read_in_english_french_italian_chinese_and_iso_forms():
    got = meta.dates("filed on 12 March 2021, published March 5, 2022, le 3 février 2019, il 7 giugno 2020, 2024.07.23 and 2023年4月14日 and 2018-01-02")
    assert got == ["2024-07-23", "2018-01-02", "2021-03-12", "2019-02-03", "2020-06-07", "2022-03-05", "2023-04-14"] or set(got) == {
        "2021-03-12", "2022-03-05", "2019-02-03", "2020-06-07", "2024-07-23", "2023-04-14", "2018-01-02"}


def test_symbols_need_the_shape_of_one_or_a_place_in_the_list_and_the_list_can_be_extended(tmp_path):
    text = "OCT4, SOX2 and KLF4 under TRE3G; Tet1, p53, IL6, Nanog and c-Myc. See FIG. 12, TABLE 4, CO2, H2O, Example 3 and the word Genes."
    got = meta.symbols(text)
    assert {"OCT4", "SOX2", "KLF4", "TRE3G", "Tet1", "p53", "IL6", "Nanog", "c-Myc"} <= set(got)
    assert not {"FIG", "TABLE", "CO2", "H2O", "Example", "Genes"} & set(got)
    assert "Foobar" not in meta.symbols("the Foobar protein")
    (tmp_path / meta.ENTITIES_FILE).write_text("# mine\nFoobar\n", encoding="utf-8")
    assert "Foobar" in meta.symbols("the foobar protein", meta.load_entities(tmp_path))


def test_quantities_keep_their_units():
    got = meta.quantities("1.5E12 vg per mouse, 2 mg/mL doxycycline, 60% survival, 3 days, 5 x 10^11 vg/kg and 10 mM citrate, 4 weeks; the year 2020 is not one")
    assert {"1.5E12 vg", "2 mg/mL", "60 %", "3 days", "10 mM", "4 weeks"} <= set(got) and not any(q.startswith("2020") for q in got)


def test_a_passage_gets_only_the_facts_it_has_with_a_language_and_the_caps_hold():
    got = meta.passage("The method of the invention is that the cell is treated with an agent for the purpose, see SEQ ID NO: 5 and 2 mg/kg of OCT4 in 2021-03-04.")
    assert got["lang"] == "en" and got["seq"] == [5] and got["years"] == [2021] and "OCT4" in got["genes"] and "2 mg/kg" in got["qty"]
    assert meta.passage("zzz qqq") == {}
    many = meta.passage(" ".join(f"GENE{i}A SEQ ID NO: {i}" for i in range(60)))
    assert len(many["genes"]) <= meta.CAPS["genes"] and len(many["seq"]) <= meta.CAPS["seq"]


def test_a_front_page_gives_the_number_title_assignee_inventors_and_date():
    chunks = [{"text": FRONT, "page_start": 1, "facts": {"lang": "en"}}, {"text": "later text", "page_start": 9, "facts": {"lang": "fr"}}]
    got = meta.document("US12274733B2.pdf", chunks)
    assert got["number"] == "US12274733B2" and got["kind"] == "patent" and got["title"] == "METHODS FOR TREATING GLAUCOMA" and got["assignee"] == "Harvard College"
    assert got["inventors"].startswith("David A. Sinclair") and got["published"] == "2023-10-12" and got["languages"] == {"en": 1, "fr": 1}
    assert meta.document("notes.md", [{"text": "hello", "page_start": 1}]) == {"kind": "document"}


def test_filters_are_checked_and_matched():
    assert meta.clean_filters(None) == {} and meta.clean_filters({"year": "2019-2023", "seq": 41.0, "language": " fr "}) == {"year": "2019-2023", "seq": 41, "language": "fr"}
    for bad in ("fr", {"colour": 1}, {"year": ["x"]}, {"language": "x" * 200}):
        with pytest.raises(RagError):
            meta.clean_filters(bad)
    passage = {"lang": "fr", "nums": ["WO2023196851A1"], "genes": ["OCT4"], "seq": [41, 42], "years": [2021], "qty": ["2 mg/kg"]}
    doc = {"language": "en", "number": "US12274733B2"}
    assert meta.matches(passage, doc, "text", {"language": "fr", "number": "wo 2023/196851", "gene": "oct4", "seq": 41, "year": "2020-2022", "unit": "mg/kg"})
    assert not meta.matches(passage, doc, "text", {"language": "en"}) and not meta.matches(passage, doc, "text", {"year": 2023})
    assert not meta.matches(passage, doc, "table", {"kind": "text"}) and meta.matches(passage, doc, "table", {"kind": "table"})
    assert meta.matches({}, doc, "text", {"language": "en", "number": "US1227"}) and not meta.matches({}, doc, "text", {"gene": "OCT4"})


def archive(tmp_path):
    store = RagStore(tmp_path / "rag")
    aid = store.create("Mixed", project=PROJ)["id"]
    store.add_bytes(aid, "WO2023196851A1.md", ("# Vectors\n\nThe vector carries OCT4 and SOX2 at SEQ ID NO: 41 in a dose of 2 mg/kg, published 12 March 2021. "
                                                  "The cassette is delivered in the eye of the animal and the vector is regulated by the drug.\n").encode())
    store.add_bytes(aid, "FR2941464A1.md", ("# Procédé\n\nLe procédé de la présente invention est une méthode pour les cellules et des gènes dans le noyau, avec une dose de 5 mg/kg "
                                              "du vecteur et de la cassette dans l'oeil de l'animal.\n").encode())
    return store, aid


def test_a_document_keeps_its_facts_and_a_search_can_be_narrowed_by_them(tmp_path):
    store, aid = archive(tmp_path)
    docs = {d["name"]: d for d in store.get(aid)["document_list"]}
    assert docs["WO2023196851A1.md"]["info"]["number"] == "WO2023196851A1" and docs["FR2941464A1.md"]["info"]["language"] == "fr"
    chunk = store.load(aid).chunks[0]
    assert chunk["facts"]["seq"] == [41] and chunk["facts"]["lang"] == "en" and "OCT4" in chunk["facts"]["genes"]
    everything = {h["document"] for h in store.search([aid], "vector cassette dose eye animal", top_k=5)["hits"]}
    assert everything == {"WO2023196851A1.md", "FR2941464A1.md"}
    fr = store.search([aid], "vector cassette dose eye animal", top_k=5, filters={"language": "fr"})["hits"]
    assert {h["document"] for h in fr} == {"FR2941464A1.md"}
    assert {h["document"] for h in store.search([aid], "vector dose", top_k=5, filters={"seq": 41})["hits"]} == {"WO2023196851A1.md"}
    assert {h["document"] for h in store.search([aid], "vector dose", top_k=5, filters={"number": "WO2023", "gene": "SOX2"})["hits"]} == {"WO2023196851A1.md"}
    assert store.search([aid], "vector dose", top_k=5, filters={"year": 1999})["hits"] == []
    with pytest.raises(RagError):
        store.search([aid], "vector", filters={"colour": "red"})


def test_the_search_tool_takes_filters_and_says_so_when_none_match(tmp_path):
    store, aid = archive(tmp_path)
    recall = ProjectRecall(store, PROJ)
    out = lookup.search(store, recall, "vector cassette dose", filters={"language": "fr"})
    assert {p["document"] for p in out["passages"]} == {"FR2941464A1.md"} and out["passages"][0]["facts"]["lang"] == "fr"
    none = lookup.search(store, recall, "vector", filters={"year": 1999})
    assert none["passages"] == [] and "filters" in none["note"]
    assert lookup.search(store, recall, "vector", filters={"zzz": 1})["status"] == "error"
    assert {p["document"] for p in lookup.search(store, recall, "vector dose", document="WO2023")["passages"]} == {"WO2023196851A1.md"}


def test_filters_go_to_the_server_only_when_there_are_some(tmp_path):
    store, aid = archive(tmp_path)
    asked = []
    store.remote = lambda ids, q, k, s, *more: asked.append(more) or {"hits": []}
    store.search([aid], "vector")
    store.search([aid], "vector", filters={"language": "fr"})
    assert asked == [(), ({"language": "fr"},)]
