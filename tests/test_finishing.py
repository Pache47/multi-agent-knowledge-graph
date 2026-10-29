"""Regression checks for independent, constrained answer-model roles."""

from types import SimpleNamespace

import pytest
from conftest import answer

from factory_assistant.assistant import LiveAssistant
from factory_assistant.assistant_contracts import ResponseDraft


def test_review_model_is_separate_and_falls_back(monkeypatch):
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            output_parsed="parsed", model=kwargs["model"], status="completed", usage=None
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("OPENAI_MODEL", "draft-model")
    monkeypatch.setenv("OPENAI_VERIFIER_MODEL", "review-model")
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    monkeypatch.setattr("factory_assistant.assistant.OpenAI", lambda **kwargs: client)
    agent = LiveAssistant()
    agent.structured("draft", {}, ResponseDraft)
    agent.structured("review", {}, ResponseDraft, reviewing=True)
    assert [c["model"] for c in calls] == ["draft-model", "review-model"]
    monkeypatch.delenv("OPENAI_VERIFIER_MODEL")
    assert LiveAssistant().verifier_model == "draft-model"


def test_answer_contract_only_accepts_ids_returned_in_this_run():
    from factory_assistant.assistant import answer_contract

    model = answer_contract(["passage:a"], [])
    claim = {"id": "x", "text": "A statement.", "citations": ["passage:b"]}
    with pytest.raises(ValueError):
        model(
            status="answered",
            findings=[claim],
            possible_causes=[],
            next_steps=[],
            question="",
            fact_ids=[],
        )
    with pytest.raises(ValueError):
        model(
            status="answered",
            findings=[],
            possible_causes=[],
            next_steps=[],
            question="",
            fact_ids=["history:case:abc"],
        )
    claim["citations"] = ["passage:a"]
    assert model(
        status="answered",
        findings=[claim],
        possible_causes=[],
        next_steps=[],
        question="",
        fact_ids=[],
    ).claims[0].citations == ["passage:a"]


def test_historical_summaries_keep_source_status_and_setup(tmp_path):
    from pathlib import Path

    from factory_assistant.assistant import response_issues
    from factory_assistant.assistant_contracts import SupportedClaim
    from factory_assistant.pipeline import build_knowledge
    from factory_assistant.query_tools import GraphTools

    root = Path(__file__).resolve().parents[1]
    tools = GraphTools(
        build_knowledge(root, tmp_path), "initial-incident", "2026-10-14T08:20:00+08:00", "CV-01"
    )
    tools.execute("find_equipment", {"query": "outfeed belt CV-01"})
    assert "CV-01" in tools.resolved
    tools.execute("query_history", {"equipment_id": "CV-01"})
    fact = tools.facts["history:case:J-071"]
    assert "reported in the source" in fact.text
    assert "Current equipment setup: B" in fact.text
    assert "These recorded setups differ" in fact.text
    assert all(c in tools.cards for c in fact.citations)
    # History cards may be cited (the verifier checks certainty); counts may not.
    explained = SupportedClaim(
        id="ok", text="J-071 removed packing material.", citations=["node:case:J-071"]
    )
    assert response_issues(answer(findings=[explained], fact_ids=[fact.id]), tools) == []
    restated = SupportedClaim(id="bad", text="A count.", citations=[fact.id])
    assert any(i.startswith("bad:") for i in response_issues(answer(findings=[restated]), tools))


def test_failed_history_draft_releases_only_source_records(tmp_path):
    from pathlib import Path

    from test_assistant import ScriptedAgent

    from factory_assistant.assistant import answer_question
    from factory_assistant.pipeline import build_knowledge

    agent = ScriptedAgent(
        calls=[
            ("find_equipment", {"query": "CV-01"}),
            ("query_history", {"equipment_id": "CV-01"}),
            ("finish_retrieval", {"outcome": "answer", "question": ""}),
        ],
        reject=True,
    )
    root = Path(__file__).resolve().parents[1]
    result = answer_question(
        build_knowledge(root, tmp_path),
        "CV-01 history",
        "2026-10-14T08:20:00+08:00",
        "initial-incident",
        tmp_path,
        mode="live",
        search_mode="keyword",
        agent=agent,
    )
    assert result.status == "evidence_only" and result.response is None
    assert len(result.facts) == 2 and all(f.kind == "maintenance_record" for f in result.facts)
    assert result.message == "Here are the matching records from the source files."
