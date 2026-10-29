"""Answering rules that code enforces: context traversal, section citations, per-claim release."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ROOT, answer
from test_assistant import ToolCall

from factory_assistant.assistant import TECHNICIAN_CARD, answer_question, response_issues
from factory_assistant.assistant_contracts import SupportedClaim
from factory_assistant.pipeline import build_knowledge
from factory_assistant.query_tools import GraphTools

INCIDENT = "2026-10-14T08:20:00+08:00"
QUESTION = "The outfeed belt stopped twice. The drive says motor overload. What should I check?"


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    return build_knowledge(ROOT, tmp_path_factory.mktemp("graph"))


def context_tools(graph):
    tools = GraphTools(graph, "initial-incident", INCIDENT, QUESTION)
    tools.find_equipment("outfeed belt")
    return tools, tools.troubleshooting_context("CV-01")


def test_one_traversal_gathers_the_incident_context(graph):
    _, context = context_tools(graph)
    assert [m["manual"]["node_id"] for m in context["applicable_manuals"]] == ["document:E5"]
    assert context["recorded_events"]["by_code"]["MOTOR_OVERLOAD"] == 2  # Duplicate counted once.
    assert [a["node_id"] for a in context["alarm_definitions"]] == ["alarm:MOTOR_OVERLOAD"]
    kinds = {o["card"]["node_id"]: o["kind"] for o in context["shift_observations"]}
    assert kinds["E3:note:6"] == "unverified suspicion"  # The operator's guess stays a guess.
    assert kinds["E3:note:5"] == "reported observation"  # Line-level note reaches its machine.
    cases = [h["record_summary_id"] for h in context["maintenance_history"]]
    assert "history:case:J-071" in cases and "history:case:WO-519" not in cases  # Later file.
    assert [p["node_id"] for p in context["procedures"]] == ["procedure:SITE-M01"]


def card(tools, prefix):
    return next(key for key in tools.cards if key.startswith(prefix))


def test_causes_and_steps_must_cite_guidance(graph):
    tools, _ = context_tools(graph)
    manual, note = card(tools, "passage:E5:"), "node:E3:note:5"
    draft = answer(
        causes=[SupportedClaim(id="c", text="Scraping may be the cause.", citations=[note])],
        steps=[SupportedClaim(id="s", text="Replace the motor.", citations=[note])],
    )
    issues = response_issues(draft, tools)
    assert any(i.startswith("c:") and "guidance that lists it" in i for i in issues)
    assert any(i.startswith("s:") and "must cite guidance" in i for i in issues)
    draft = answer(
        causes=[SupportedClaim(id="c", text="Mechanical resistance.", citations=[manual])],
        steps=[SupportedClaim(id="s", text="Follow the procedure.", citations=[manual])],
    )
    assert response_issues(draft, tools) == []


class Responder:
    """Scripted investigator, responder and verifier; the verifier rejects chosen claims."""

    model = "test-double"

    def __init__(self, draft_for, reject=()):
        self.calls = iter(
            [
                ("find_equipment", {"query": "outfeed belt"}),
                ("troubleshooting_context", {"equipment_id": "CV-01"}),
                ("finish_retrieval", {"outcome": "answer", "question": ""}),
            ]
        )
        self.draft_for, self.reject, self.drafts = draft_for, set(reject), 0

    def choose_tool(self, conversation, definitions):
        name, arguments = next(self.calls)
        return SimpleNamespace(output=[ToolCall(name=name, arguments=json.dumps(arguments))])

    def draft(self, question, tools, feedback):
        self.drafts += 1
        return self.draft_for(tools)

    def verify(self, question, tools, draft):
        return [f"{c.id}: not supported" for c in draft.claims if c.id in self.reject]


def run(graph, agent, tmp_path, question=QUESTION):
    return answer_question(
        graph,
        question,
        INCIDENT,
        "initial-incident",
        Path(tmp_path),
        mode="live",
        search_mode="keyword",
        agent=agent,
    )


def troubleshooting_draft(tools):
    manual = card(tools, "passage:E5:")
    return answer(
        findings=[
            SupportedClaim(
                id="a", text="Scraping was reported near the outfeed.", citations=["node:E3:note:5"]
            ),
            SupportedClaim(id="b", text="The motor is worn out.", citations=["node:E3:note:6"]),
        ],
        causes=[
            SupportedClaim(id="c", text="Mechanical resistance is possible.", citations=[manual])
        ],
        steps=[
            SupportedClaim(id="d", text="Check for resistance under SITE-M01.", citations=[manual])
        ],
        question="Where was the scraping heard?",
        fact_ids=[next(k for k in tools.facts if k.startswith("events:"))],
    )


def test_only_unverified_claims_are_removed_and_the_boundary_is_added(graph, tmp_path):
    agent = Responder(troubleshooting_draft, reject={"findings-2"})
    result = run(graph, agent, tmp_path)
    assert result.status == "answered" and agent.drafts == 2  # One correction was tried.
    assert [c.text for c in result.response.findings] == ["Scraping was reported near the outfeed."]
    assert result.response.question == "Where was the scraping heard?"
    assert "1 statement(s) could not be verified" in result.message
    boundary = next(f for f in result.facts if f.kind == "safety_boundary")
    assert boundary.id in result.response.fact_ids
    assert "SITE-M01" in boundary.text and "ask the maintenance lead" in boundary.text


def test_technician_report_is_citable_but_is_not_guidance(graph, tmp_path):
    def draft(tools):
        return answer(
            findings=[
                SupportedClaim(
                    id="a", text="You found a stiff roller.", citations=[TECHNICIAN_CARD]
                )
            ],
        )

    result = run(graph, Responder(draft), tmp_path, QUESTION + " We found a stiff roller.")
    assert result.status == "answered" and result.response.findings[0].citations == [
        TECHNICIAN_CARD
    ]
    assert not any(f.kind == "safety_boundary" for f in result.facts)  # No next steps given.


def test_document_fallback_reaches_text_the_graph_does_not_model(graph):
    tools = GraphTools(graph, "initial-incident", INCIDENT, "Can I use the spare photoeye?")
    found = tools.search_documents("spare photoeye compatibility checked", 6)
    texts = [r["card"]["text"] for r in found["results"]]
    assert any("compatibility with any installed sensor has not been checked" in t for t in texts)
    cited = {
        (r["source_id"], r["excerpt"]) for res in found["results"] for r in res["card"]["evidence"]
    }
    # Stock numbers come only from the reviewed stock tool; manuals only through their
    # applicability path; later files are not yet known.
    assert not any(sid == "E7b" and any(c.isdigit() for c in text) for sid, text in cited)
    sources = {sid for sid, _ in cited}
    assert not sources & {"E5", "E6", "N1", "N2", "N3", "C3", "N4"}


def test_a_file_with_an_unresolved_machine_comes_back_labelled_as_held(graph):
    later = GraphTools(graph, "initial-incident", "2099-01-01T00:00:00+00:00", "callout")
    found = later.search_documents("13 October late shift callout outfeed stopping", 6)
    cards = [r["card"] for r in found["results"]]
    held = [c for c in cards if any(r["source_id"] == "N3" for r in c["evidence"])]
    assert held and all(c["title"].startswith("Held record") for c in held)
    assert held[0]["text"].startswith("HELD RECORD") and "WO-527" in held[0]["text"]
    others = [c for c in cards if c not in held]
    assert all(c["title"].startswith("Source text") for c in others)


def test_a_stock_note_may_be_cited_but_a_stock_number_may_not(graph):
    tools = GraphTools(graph, "initial-incident", INCIDENT, "spare photoeye for CV-01")
    found = tools.search_documents("photoeye compatibility not checked", 6)
    note = next(
        r["card"]["id"] for r in found["results"] if "not been checked" in r["card"]["text"]
    )
    claim = SupportedClaim(
        id="findings-1", text="The spare's compatibility is unchecked.", citations=[note]
    )
    assert response_issues(answer(findings=[claim]), tools) == []
    tools.find_equipment("CV-01")
    tools.query_stock("CV-01")
    row = next(k for k in tools.cards if k.startswith("node:stock:"))
    claim = SupportedClaim(id="findings-1", text="One is in stock.", citations=[row])
    assert "stock records are shown" in " ".join(response_issues(answer(findings=[claim]), tools))


def test_an_empty_graph_investigation_falls_back_to_the_source_text(graph, tmp_path):
    def draft(tools):
        note = next(
            k
            for k, c in tools.cards.items()
            if k.startswith("source:") and "not been checked" in c.text
        )
        return answer(
            findings=[
                SupportedClaim(id="a", text="The spare's fit is unchecked.", citations=[note])
            ]
        )

    agent = Responder(draft)
    agent.calls = iter([("finish_retrieval", {"outcome": "insufficient_evidence", "question": ""})])
    result = run(graph, agent, tmp_path, "Can I use the spare photoeye in stores for PE-02?")
    assert [t["tool"] for t in result.trace if t.get("tool")][-1] == "search_documents"
    assert result.status == "answered" and agent.drafts == 1
