"""The document baseline sees the same files at the same cutoff, with exact citations."""

import pytest
from conftest import ROOT

from factory_assistant.baseline import DocumentTools

INCIDENT = "2026-10-14T08:20:00+08:00"


def test_cutoff_pages_and_citations():
    early = DocumentTools(ROOT, "main", INCIDENT, None)
    assert "E7b" in early.sources and "N4" not in early.sources  # Later files are excluded.
    later = DocumentTools(ROOT, "main", "2026-10-18T12:00:00+08:00", None)
    pdf = [c for c in later.chunks if c.evidence[0].source_id == "N4"]
    assert {r.page for c in pdf for r in c.evidence} == {1, 2}
    assert all(len({r.page for r in c.evidence}) == 1 for c in pdf)  # Never across pages.
    card = early.read_document("E2", 3, 2)["card"]
    lines = early.sources["E2"].text.splitlines()
    assert [r["excerpt"] for r in card["evidence"]] == lines[2:4]
    assert early.passages(None) == []  # No triage, so no standing policy context.
    with pytest.raises(ValueError):
        early.read_document("evaluation", 1, 1)


def test_search_returns_cited_passages():
    tools = DocumentTools(ROOT, "main", INCIDENT, None)
    results = tools.search_documents("MOTOR_OVERLOAD", 3)["results"]
    assert results and all(r["card"]["id"] in tools.cards for r in results)


def test_a_baseline_clarification_gets_the_general_question():
    from factory_assistant.assistant import clarification

    tools = DocumentTools(ROOT, "main", INCIDENT, None)
    assert clarification(tools).startswith("Which machine, record or time")
