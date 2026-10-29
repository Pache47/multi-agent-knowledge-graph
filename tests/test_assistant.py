"""Exercise the new query engine's boundaries without calling a model or an embedding API."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import answer
from pydantic import BaseModel

from factory_assistant.assistant import answer_question
from factory_assistant.assistant_contracts import SupportedClaim
from factory_assistant.passage_search import EmbeddingCache, search_passages
from factory_assistant.query_tools import GraphTools
from factory_assistant.scenarios import load_scenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario():
    return load_scenario(ROOT, "sensor_stop")


def reader(scenario, question="carton buffer"):
    metadata, bundle = scenario
    return GraphTools(bundle, metadata["dataset"], metadata["question_at"], question)


def test_graph_walk_has_two_hop_manual_path_and_exact_citations(scenario):
    tools = reader(scenario)
    assert tools.find_equipment("carton buffer")["resolved"]
    walked = tools.execute(
        "walk_graph",
        {
            "start_id": "BF-10",
            "relations": ["HAS_CONFIGURATION", "APPLIES_TO"],
            "direction": "both",
            "hops": 2,
        },
    )
    manual = next(p for p in walked["paths"] if p["node"]["node_id"] == "document:S3")
    assert len(manual["path"]) == 2
    assert manual["path_citation"] in tools.cards
    assert "HAS_CONFIGURATION" in tools.cards[manual["path_citation"]].text
    assert all(step["citation"] in tools.cards for step in manual["path"])
    assert tools.available(tools.cards[manual["node"]["id"]].evidence)
    with pytest.raises(ValueError):
        tools.execute(
            "walk_graph",
            {
                "start_id": "BF-10",
                "relations": [],
                "direction": "both",
                "hops": 4,
            },
        )


def test_candidate_cannot_be_promoted_by_an_invented_user_identity(scenario):
    tools = reader(scenario, "The conveyor stopped")
    assert not tools.find_equipment("conveyor")["resolved"]
    assert not tools.find_equipment("BF-10")["resolved"]
    with pytest.raises(ValueError):
        tools.query_events("BF-10", None, None)
    with pytest.raises(ValueError):
        tools.search_passages("detector", "BF-10", 3)
    # A subsequent turn includes the technician's own clarification.
    tools = reader(scenario, "The conveyor stopped. Technician clarification: BF-10")
    assert tools.find_equipment("BF-10")["resolved"]


def test_counts_duplicates_filters_and_unknown_are_distinct(scenario):
    tools = reader(scenario)
    tools.find_equipment("carton buffer")
    assert tools.query_events("BF-10", "SENSOR_BLOCKED", None)["count"] == 3
    normalized = tools.query_events("BF-10", "sensor blocked", None)
    assert normalized["count"] == 3 and normalized["event_code"] == "SENSOR_BLOCKED"
    assert tools.query_events("BF-10", "SENSOR_BLOCKED", "2026-10-17T14:00:00+08:00")["count"] == 2
    # A code that exists nowhere in the records is unknown, not a count of zero.
    with pytest.raises(ValueError, match="Codes recorded for this equipment: SENSOR_BLOCKED"):
        tools.query_events("BF-10", "MOTOR_OVERLOAD", None)
    distant_start = tools.query_events("BF-10", "SENSOR_BLOCKED", "0001-01-01T00:00:00+00:00")
    assert "the available history" in distant_start["computed_fact"]["text"]
    tools.walk_graph("BF-10", ["HAS_COMPONENT"], "outgoing", 1)
    unknown = tools.query_events("DRV-10", None, None)
    assert unknown["count"] is None and unknown["coverage"] == "no_related_log_evidence"
    with pytest.raises(ValueError):
        tools.query_events("BF-10", None, "2026-10-18T00:00:00+08:00")


def test_filters_run_before_embedding_and_policy_search_has_no_machine_text(scenario):
    tools = reader(scenario)
    tools.find_equipment("carton buffer")
    tools.edges = [edge for edge in tools.edges if edge.relation != "APPLIES_TO"]
    seen = []

    class CapturingEmbeddings:
        def vectors(self, texts):
            seen.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    tools.embeddings = CapturingEmbeddings()
    tools.search_passages("guidance", "BF-10", 6)
    assert not any("Detection timeout" in text for text in seen)
    seen.clear()
    tools.search_passages("maintenance", None, 6)
    assert not any("DETECT-81" in text for text in seen)
    assert any("SITE-M02" in text for text in seen)
    metadata, bundle = scenario
    future = GraphTools(bundle, metadata["dataset"], "2026-10-17T13:00:00+08:00", "BF-10")
    assert "S2" not in future.documents
    assert all(ref.source_id != "S2" for card in future.passages(None) for ref in card.evidence)
    other = GraphTools(bundle, "another-dataset", metadata["question_at"], "BF-10")
    assert not other.nodes and not other.documents


def test_tampered_source_reference_is_excluded(scenario):
    metadata, bundle = scenario
    bundle = bundle.model_copy(deep=True)
    next(n for n in bundle.nodes if n.id == "BF-10").evidence[0].excerpt = "fabricated"
    tools = reader((metadata, bundle))
    assert tools.find_equipment("BF-10")["matches"] == []


class RenamingAgents:
    """Replays recorded decisions for renamed files, renaming the decisions the same way.

    This stands in for agents reading the renamed text. Every renamed decision still
    goes through the same code checks, parsers and identity resolution.
    """

    def __init__(self, renames):
        from factory_assistant.agents import ModelAgents

        self.inner = ModelAgents(ROOT / "agent_cache", "replay")
        self.renames = renames

    def swap(self, text, reverse=False):
        for old, new in self.renames.items():
            text = text.replace(new, old) if reverse else text.replace(old, new)
        return text

    def original(self, source):
        relative = source.path.replace("renamed", "sensor_stop")
        return source.model_copy(update={"text": self.swap(source.text, True), "path": relative})

    def renamed(self, decision):
        return type(decision).model_validate_json(self.swap(decision.model_dump_json()))

    def triage(self, source):
        decision, meta = self.inner.triage(self.original(source))
        return self.renamed(decision), meta

    def extract(self, doc_type, source, previous=None, feedback=None):
        if previous is not None:
            previous = type(previous).model_validate_json(
                self.swap(previous.model_dump_json(), True)
            )
        feedback = [self.swap(item, True) for item in feedback or []] or None
        decision, meta = self.inner.extract(doc_type, self.original(source), previous, feedback)
        return self.renamed(decision), meta

    def reconcile(self, source, mentions, registry):
        def restore(value):
            return json.loads(self.swap(json.dumps(value), True))

        decision, meta = self.inner.reconcile(
            self.original(source), restore(mentions), restore(registry)
        )
        return self.renamed(decision), meta


def test_equipment_alarm_and_manual_can_change_without_code_changes(tmp_path):
    from factory_assistant.evidence import load_manifest
    from factory_assistant.pipeline import build_knowledge

    folder = tmp_path / "data/scenarios/renamed"
    shutil.copytree(ROOT / "data/scenarios/sensor_stop", folder)
    renames = {"BF-10": "ZZ-90", "SENSOR_BLOCKED": "BEAM_TIMEOUT"}
    for path in folder.iterdir():
        text = path.read_text()
        for old, new in renames.items():
            text = text.replace(old, new)
        path.write_text(text)
    agents = RenamingAgents(renames)
    bundle = build_knowledge(tmp_path, tmp_path / "rt", "replay", "data/scenarios/renamed", agents)
    metadata = load_manifest(tmp_path, "data/scenarios/renamed")
    tools = GraphTools(bundle, metadata["dataset"], metadata["question_at"], "ZZ-90")
    assert tools.find_equipment("ZZ-90")["resolved"]
    assert tools.query_events("ZZ-90", "BEAM_TIMEOUT", None)["count"] == 3
    assert any("BEAM_TIMEOUT" in c.text for c in tools.passages("ZZ-90"))
    assert not bundle.held


def test_embedding_cache_reuses_text_but_separates_models(tmp_path):
    calls = []

    class FakeEmbeddingAPI:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=i, embedding=[float(len(text)), 1.0])
                    for i, text in enumerate(kwargs["input"])
                ]
            )

    client = SimpleNamespace(embeddings=FakeEmbeddingAPI())
    path = tmp_path / "vectors.sqlite"
    cache = EmbeddingCache(path, client, "model-one")
    first = cache.vectors(["a", "longer", "a"])
    assert first[0] == first[2] and len(calls[0]["input"]) == 2
    assert cache.vectors(["longer", "a"]) == [first[1], first[0]]
    assert len(calls) == 1
    EmbeddingCache(path, client, "model-two").vectors(["a"])
    assert len(calls) == 2


def test_semantic_search_can_retrieve_without_shared_words(scenario):
    tools = reader(scenario)
    tools.find_equipment("carton buffer")
    cards = [c for c in tools.passages("BF-10") if "Detection timeout" in c.text]
    assert cards and search_passages("photoelectric malfunction", cards, 1) == []

    class SameMeaning:
        def vectors(self, texts):
            return [[1.0, 0.0] for _ in texts]

    results = search_passages("photoelectric malfunction", cards, 1, SameMeaning())
    assert results[0]["lexical_matches"] == 0
    assert results[0]["cosine_similarity"] == 1.0


class ToolCall(BaseModel):
    type: str = "function_call"
    name: str
    arguments: str
    call_id: str = "test-call"


def test_evidence_gap_does_not_echo_the_question_as_a_clarification(scenario, tmp_path):
    metadata, bundle = scenario
    agent = ScriptedAgent(
        calls=[
            (
                "finish_retrieval",
                {"outcome": "insufficient_evidence", "question": "Is it in stock?"},
            )
        ]
    )
    result = answer_question(
        bundle,
        "Is it in stock?",
        metadata["question_at"],
        metadata["dataset"],
        tmp_path,
        mode="live",
        search_mode="keyword",
        agent=agent,
    )
    assert result.status == "insufficient_evidence"
    assert result.response.question == ""
    assert result.response.fact_ids == []


class ScriptedAgent:
    """Supply decisions, not fake model claims of success; assertions inspect real tool outputs."""

    model = "test-double"

    def __init__(self, calls=None, reject=False, bad_citation=False):
        self.calls = iter(
            calls
            or [
                ("find_equipment", {"query": "carton buffer"}),
                (
                    "query_events",
                    {"equipment_id": "BF-10", "event_code": "SENSOR_BLOCKED", "since": None},
                ),
                ("finish_retrieval", {"outcome": "answer", "question": ""}),
            ]
        )
        self.reject, self.bad_citation = reject, bad_citation
        self.drafts, self.reviews = 0, 0

    def choose_tool(self, conversation, definitions):
        name, arguments = next(self.calls)
        return SimpleNamespace(output=[ToolCall(name=name, arguments=json.dumps(arguments))])

    def draft(self, question, tools, feedback):
        self.drafts += 1
        claims = []
        if self.bad_citation and not feedback:
            claims = [SupportedClaim(id="c1", text="Unreviewed draft", citations=["not-returned"])]
        return answer(findings=claims, fact_ids=list(tools.facts))

    def verify(self, question, tools, draft):
        self.reviews += 1
        assert all(
            citation in tools.cards for claim in draft.claims for citation in claim.citations
        )
        return ["Test rejection"] if self.reject else []


def run(scenario, tmp_path, agent):
    metadata, bundle = scenario
    return answer_question(
        bundle,
        metadata["question"],
        metadata["question_at"],
        metadata["dataset"],
        tmp_path,
        mode="live",
        search_mode="keyword",
        agent=agent,
    )


def test_agent_selects_tools_and_code_facts_survive_without_graph_mutation(scenario, tmp_path):
    before = scenario[1].fingerprint
    result = run(scenario, tmp_path, ScriptedAgent())
    assert result.status == "answered" and len(result.response.fact_ids) == 1
    assert "3 matching recorded events" in result.facts[0].text
    assert scenario[1].fingerprint == before


def test_invalid_reference_corrected_before_verifier_sees_it(scenario, tmp_path):
    agent = ScriptedAgent(bad_citation=True)
    result = run(scenario, tmp_path, agent)
    first = next(t for t in result.trace if t.get("stage") == "verify")
    assert result.status == "answered" and agent.drafts == 2
    assert "unknown citation" in first["issues"][0]


def test_persistent_rejection_withholds_after_one_correction(scenario, tmp_path):
    agent = ScriptedAgent(reject=True)
    result = run(scenario, tmp_path, agent)
    assert result.status == "withheld" and result.response is None and agent.drafts == 2


def test_ambiguous_question_returns_reviewed_clarification(scenario, tmp_path):
    agent = ScriptedAgent(
        calls=[
            (
                "finish_retrieval",
                {"outcome": "clarify", "question": "Which equipment ID do you mean?"},
            )
        ]
    )
    result = run(scenario, tmp_path, agent)
    # Nothing was drafted or reviewed, so the technician gets a neutral question from code.
    assert result.status == "clarification" and agent.reviews == 0 and agent.drafts == 0
    assert result.response.question.startswith("Which machine")


def test_a_clarification_cannot_carry_unreviewed_facts(scenario, tmp_path):
    claim = "We have 99 confirmed failures on BF-10. Which shift saw them?"
    agent = ScriptedAgent(
        calls=[
            ("find_equipment", {"query": "conveyor"}),
            ("finish_retrieval", {"outcome": "clarify", "question": claim}),
        ]
    )
    result = run(scenario, tmp_path, agent)
    assert result.status == "clarification" and "99" not in result.response.question
    assert result.response.question.startswith("Which machine")


def test_the_responders_question_is_reviewed_and_dropped_if_unsupported(scenario, tmp_path):
    class Asking(ScriptedAgent):
        def draft(self, question, tools, feedback):
            return answer(
                question="Since the detector failed 99 times, which one?",
                fact_ids=list(tools.facts),
            )

        def verify(self, question, tools, draft):
            self.reviewed = [c.id for c in draft.claims]
            return ["question: assumes 99 failures, which no card states."]

    agent = Asking()
    agent.drafts = 0
    result = run(scenario, tmp_path, agent)
    assert "question" in agent.reviewed
    # The rest of the answer is released; only the question is left out.
    assert result.status == "answered" and result.response.question == ""


def test_a_draft_that_answers_is_not_labelled_a_clarification(scenario, tmp_path):
    class Answering(ScriptedAgent):
        def draft(self, question, tools, feedback):
            return answer(
                question="Which shift saw it?", fact_ids=list(tools.facts), status="clarification"
            )

    result = run(scenario, tmp_path, Answering())
    assert result.status == "answered" and result.response.question == "Which shift saw it?"


def test_tool_budget_and_repeated_calls_are_bounded(scenario, tmp_path):
    agent = ScriptedAgent(calls=[("find_equipment", {"query": "carton buffer"})] * 10)
    result = run(scenario, tmp_path, agent)
    # The last decision permits only finish; a model cannot bypass that tool schema.
    assert result.status == "withheld" and len(result.trace) == 10
    assert "already returned" in result.trace[1]["result"]["error"]


def test_model_failure_is_withheld(scenario, tmp_path):
    class Broken(ScriptedAgent):
        def verify(self, *args):
            raise ValueError("Incomplete model response")

    result = run(scenario, tmp_path, Broken())
    assert result.status == "withheld" and result.response is None


def test_live_verifier_sees_only_each_claims_cited_cards(scenario):
    from factory_assistant.answer_models import ClaimReview
    from factory_assistant.assistant import LiveAssistant

    tools = reader(scenario)
    tools.find_equipment("carton buffer")
    tools.search_passages("detection", "BF-10", 4)
    card_id = next(key for key in tools.cards if key.startswith("passage:S3:"))
    draft = answer(
        findings=[SupportedClaim(id="interpretation", text="Test claim", citations=[card_id])]
    )
    contexts = []

    class CapturingVerifier(LiveAssistant):
        def __init__(self):
            pass  # No client or secret needed for the contract test.

        def structured(self, instructions, context, model, *, reviewing=False):
            contexts.append(context)
            assert model is ClaimReview
            assert {card["id"] for card in context["cited_cards"]} == {card_id}
            assert "evidence" not in context
            return ClaimReview(claim_id=context["claim_id"], supported=True, reason="Test")

    assert CapturingVerifier().verify("Test question", tools, draft) == []
    assert len(contexts) == 1  # One isolated review per claim; no whole-answer review.


def test_tool_schemas_are_strict_and_documented(scenario):
    definitions = reader(scenario).definitions()
    assert len(definitions) == 10
    for definition in definitions:
        assert definition["strict"] and len(definition["description"]) > 40
        schema = definition["parameters"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(schema["required"])
