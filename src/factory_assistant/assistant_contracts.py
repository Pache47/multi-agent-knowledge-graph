"""Incident-independent records shared by tools, the assistant and the UI.

These contracts describe an evidence-supported response, not a particular fault,
manual layout, equipment name or number of events.
"""

from typing import Literal

from pydantic import Field

from factory_assistant.answer_models import EvidenceCard
from factory_assistant.models import Record


class ComputedFact(Record):
    """A tool-calculated statement, rendered verbatim rather than rewritten by a model."""

    id: str
    text: str
    citations: list[str]
    kind: Literal[
        "event_count", "maintenance_record", "stock_record", "source_quote", "safety_boundary"
    ] = "event_count"


class SupportedClaim(Record):
    """One short statement and the evidence cards that support every part of it."""

    id: str
    text: str = Field(
        min_length=1,
        max_length=380,
        description="One short statement in plain language, fully supported by its citations.",
    )
    citations: list[str] = Field(min_length=1, max_length=6)


SECTIONS = ("findings", "possible_causes", "next_steps")


class ResponseDraft(Record):
    """A technician-facing answer. Sections may be empty; missing evidence is a valid outcome."""

    status: Literal["answered", "clarification", "insufficient_evidence"]
    findings: list[SupportedClaim] = Field(
        max_length=5, description="What the records show that answers the question."
    )
    possible_causes: list[SupportedClaim] = Field(
        max_length=4, description="Explanations the cited guidance lists; never a diagnosis."
    )
    next_steps: list[SupportedClaim] = Field(
        max_length=4, description="What to check or consult next, per cited guidance."
    )
    question: str = Field(
        max_length=300,
        description="One question for the technician about what they have seen or found, or empty.",
    )
    fact_ids: list[str] = Field(max_length=6)

    @property
    def claims(self) -> list[SupportedClaim]:
        return [claim for section in SECTIONS for claim in getattr(self, section)]


class AssistantResult(Record):
    """Serializable run record. Failed drafts belong only in the review trace."""

    status: Literal[
        "answered", "clarification", "insufficient_evidence", "withheld", "evidence_only"
    ]
    question: str
    as_of: str
    dataset: str
    mode: Literal["offline", "live"]
    search_mode: Literal["keyword", "hybrid"]
    model: str | None
    verifier_model: str | None = None
    graph_fingerprint: str
    message: str
    response: ResponseDraft | None
    facts: list[ComputedFact]
    evidence: list[EvidenceCard]
    trace: list[dict]
