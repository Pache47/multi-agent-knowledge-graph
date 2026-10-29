"""File boundaries and register validation, independent of any incident vocabulary."""

from pathlib import Path

import pytest
from pypdf import PdfWriter

from factory_assistant.models import SourceSnapshot
from factory_assistant.parsers import parse_equipment_csv
from factory_assistant.source_readers import read_source

ROOT = Path(__file__).resolve().parents[1]


def test_csv_citations_follow_physical_lines_even_with_quoted_newlines():
    text = (
        "id,name,aliases,parent,model,configuration\n"
        'X-9,"Process\npump",utility feed,,PX,Z\n'
        "V-3,Valve,,X-9,,\n"
    )
    source = SourceSnapshot(
        source_id="CUSTOM",
        path="data/custom.csv",
        sha256="test",
        text=text,
        registered_at="2026-10-18T08:00:00+08:00",
    )
    assets = parse_equipment_csv(source)
    valve = next(a for a in assets if a.id == "V-3")
    assert {r.line for r in valve.evidence} == {1, 4}
    assert all(r.excerpt == text.splitlines()[r.line - 1] for a in assets for r in a.evidence)
    assert valve.parent == "X-9"


@pytest.mark.parametrize("damage", ["parent", "partial_config", "columns"])
def test_invalid_register_rows_are_held_not_guessed(tmp_path, damage):
    from conftest import FakeAgents, make_folder

    from factory_assistant.pipeline import build_knowledge

    text = (ROOT / "data/shared_folder/filling_equipment.csv").read_text()
    if damage == "parent":
        text = text.replace(",FILL-20,", ",MISSING-1,")
    elif damage == "partial_config":
        text = text.replace("TP-30,H2", "TP-30,")
    else:
        text = text.replace("aliases", "nicknames")
    folder = make_folder(tmp_path, {"register.csv": text})
    agents = FakeAgents(
        triage={"F1": "equipment_list"},
        extract={("F1", "equipment_list"): [{"assets": [], "flows": []}]},
    )
    bundle = build_knowledge(tmp_path, tmp_path / "runtime", "replay", folder, agents=agents)
    if damage == "parent":
        assert "P-21" in {n.id for n in bundle.nodes}
        assert any("MISSING-1 is not known" in h["reason"] for h in bundle.held)
    else:
        # The parser declines the whole file; the extraction agent gets it instead.
        steps = bundle.extraction_trace[0]["steps"]
        assert any("fallback" in s for s in steps)
        assert bundle.extraction_trace[0]["method"] == "agent: extraction"


@pytest.mark.parametrize("kind", ["blank", "encrypted", "broken"])
def test_unreadable_pdf_is_not_silently_ingested(tmp_path, kind):
    path = tmp_path / "data/attachment.pdf"
    path.parent.mkdir()
    if kind == "broken":
        path.write_bytes(b"This is not a PDF")
    else:
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        if kind == "encrypted":
            writer.encrypt("example-password")
        writer.write(path)
    with pytest.raises(ValueError, match="OCR|Encrypted|could not be read"):
        read_source(
            tmp_path,
            {"id": "X", "path": "data/attachment.pdf", "recorded_at": "2026-10-18T08:00:00+08:00"},
        )


def test_reader_rejects_path_outside_registered_data():
    entry = {"id": "N4", "path": "evaluation/README.md", "recorded_at": "2026-10-18T08:00:00+08:00"}
    with pytest.raises(ValueError, match="within data"):
        read_source(ROOT, entry)
