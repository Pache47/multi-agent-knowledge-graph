"""Small contracts shared by the query tools and the claim verifier."""

from factory_assistant.graph_models import Evidence
from factory_assistant.models import Record


class EvidenceCard(Record):
    """One piece of retrieved evidence: its text and the exact source lines behind it."""

    id: str
    title: str
    text: str
    evidence: list[Evidence]


class ClaimReview(Record):
    claim_id: str
    supported: bool
    reason: str
