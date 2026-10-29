"""Shared test helpers. No test calls a model: agents are scripted or replayed from the cache."""

import json
import shutil
from pathlib import Path

import pytest

from factory_assistant.assistant_contracts import ResponseDraft
from factory_assistant.contracts import EXTRACTION_CONTRACTS, Reconciliation, Triage

ROOT = Path(__file__).resolve().parents[1]
RECORDED = "2026-10-18T08:00:00+08:00"


def answer(findings=(), causes=(), steps=(), question="", fact_ids=(), status="answered"):
    """A responder draft with the sectioned answer contract."""
    return ResponseDraft(
        status=status,
        findings=list(findings),
        possible_causes=list(causes),
        next_steps=list(steps),
        question=question,
        fact_ids=list(fact_ids),
    )


class FakeAgents:
    """Scripted construction agents keyed by source ID; every call is recorded.

    extract[source] is a list: the first item answers the first extraction, the next
    answers the correction. reconcile maps a mention to (decision, equipment_id).
    """

    def __init__(self, triage=None, extract=None, reconcile=None, alternative=None):
        self.triage_types = triage or {}
        self.alternatives = alternative or {}
        self.extractions = extract or {}
        self.reconciliations = reconcile or {}
        self.calls = []

    def triage(self, source):
        self.calls.append(("triage", source.source_id))
        return (
            Triage(
                doc_type=self.triage_types.get(source.source_id, "unsupported"),
                alternative=self.alternatives.get(source.source_id),
                reason="scripted",
            ),
            {"agent": "triage", "model": "fake", "cached": False},
        )

    def extract(self, doc_type, source, previous=None, feedback=None):
        self.calls.append(("extract", source.source_id, doc_type, tuple(feedback or ())))
        answers = self.extractions[(source.source_id, doc_type)]
        answer = answers[min(len(answers) - 1, 1 if feedback else 0)]
        decision = EXTRACTION_CONTRACTS[doc_type].model_validate(answer)
        return decision, {"agent": "extract", "model": "fake", "cached": False}

    def reconcile(self, source, mentions, registry):
        self.calls.append(("reconcile", source.source_id, [m["mention"] for m in mentions]))
        decisions = []
        for item in mentions:
            decision, identity = self.reconciliations.get(item["mention"], ("unresolved", None))
            decisions.append(
                {
                    "mention_index": item["mention_index"],
                    "decision": decision,
                    "equipment_id": identity,
                    "reason": "scripted",
                }
            )
        return (
            Reconciliation(decisions=decisions),
            {"agent": "reconcile", "model": "fake", "cached": False},
        )


def make_folder(root: Path, files: dict[str, str], name: str = "test_folder") -> str:
    """Register files in a fresh folder under data/, like a customer's shared drive."""
    folder = root / "data" / name
    folder.mkdir(parents=True, exist_ok=True)
    sources = []
    for index, (file, text) in enumerate(files.items(), 1):
        (folder / file).write_text(text)
        sources.append({"id": f"F{index}", "file": file, "recorded_at": RECORDED})
    manifest = {
        "dataset": "test",
        "title": "Test folder",
        "timezone": "Asia/Singapore",
        "question": "Test question",
        "question_at": RECORDED,
        "sources": sources,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return f"data/{name}"


@pytest.fixture
def project(tmp_path):
    """A copy of the real data and recorded agent decisions; tests may change it freely."""
    shutil.copytree(ROOT / "data", tmp_path / "data")
    shutil.copytree(ROOT / "agent_cache", tmp_path / "agent_cache")
    shutil.copytree(ROOT / "evaluation", tmp_path / "evaluation")
    return tmp_path
