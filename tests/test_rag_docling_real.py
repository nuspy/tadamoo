"""One check of the real Docling worker, on the CPU, with a two-page PDF made here. Skipped when the CPU environment of Docling is not on this PC.
It loads Docling's models, so it takes a minute or two; it never asks the server for the GPU (`cpu=True`)."""
from pathlib import Path

import pytest

from tadamoo.rag.ingest import docling_client

CPU_PYTHON = Path("E:/Dev/venvs/docling/Scripts/python.exe")


def tiny_pdf(pages):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [" + " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages))) + f"] /Count {len(pages)} >>"]
    for i, lines in enumerate(pages):
        content = "BT /F1 14 Tf 72 720 Td 18 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + i * 2} 0 R /Resources << /Font << /F1 {3 + len(pages) * 2} 0 R >> >> >>")
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    table = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n".encode()


@pytest.mark.skipif(not CPU_PYTHON.is_file(), reason="the CPU environment of Docling is not on this PC")
def test_the_real_worker_reads_a_two_page_pdf_on_the_cpu_in_windows_of_one_page(tmp_path):
    pdf = tmp_path / "two.pdf"
    pdf.write_bytes(tiny_pdf([["Harbour Master Notes", "The tide table is kept in the red cabinet."], ["Second Page", "Moorings cost ten coins a night."]]))
    seen = []
    got = docling_client.read(str(CPU_PYTHON), pdf, ".pdf", progress=seen.append, window=1, cpu=True, idle_s=900, total_s=1500)
    assert got["pages"] == 2 and got["device"] == "cpu"
    text = {b["page"]: " ".join(x["text"] for x in got["blocks"] if x["page"] == b["page"]) for b in got["blocks"] if b["type"] != "page_break"}
    assert "tide table" in text[1] and "Moorings" in text[2]
    assert [e["event"] for e in seen][0] == "start" and [e["page"] for e in seen if e["event"] == "progress"] == [1, 2] and seen[-1]["event"] == "done"
    assert {b["type"] for b in got["blocks"]} >= {"paragraph", "page_break"}
