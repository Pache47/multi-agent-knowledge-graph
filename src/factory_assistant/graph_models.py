"""Small graph contract: validated facts retain their exact source evidence."""

import hashlib
import json
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr

from factory_assistant.models import Record


class Evidence(Record):
    source_id: str
    path: str
    sha256: str
    line: int = Field(ge=1)
    excerpt: str
    page: int | None = Field(default=None, ge=1)


class Node(Record):
    id: str
    kind: Literal[
        "Equipment",
        "Configuration",
        "Event",
        "AlarmType",
        "Part",
        "StockRecord",
        "Document",
        "ProcedureReference",
        "MaintenanceCase",
        "Finding",
    ]
    properties: dict[str, StrictStr | StrictInt | StrictFloat | StrictBool | list[str]]
    evidence: list[Evidence] = Field(min_length=1)


class Edge(Record):
    source: str
    relation: Literal[
        "HAS_COMPONENT",
        "SENDS_TO",
        "HAS_CONFIGURATION",
        "RECORDED_FOR",
        "REPORTS_ALARM",
        "STOCK_OF",
        "APPLIES_TO",
        "REFERENCES",
        "DESCRIBES",
        "HAS_FINDING",
        "APPROVED_FOR",
        "DEFINES",
    ]
    target: str
    evidence: list[Evidence] = Field(min_length=1)


# Direction and endpoint types are controlled by code, never model-generated Cypher.
EDGE_TYPES = {
    "HAS_COMPONENT": ("Equipment", "Equipment"),
    "SENDS_TO": ("Equipment", "Equipment"),
    "HAS_CONFIGURATION": ("Equipment", "Configuration"),
    "RECORDED_FOR": ("Event", "Equipment"),
    "REPORTS_ALARM": ("Event", "AlarmType"),
    "STOCK_OF": ("StockRecord", "Part"),
    "APPLIES_TO": ("Document", "Configuration"),
    "REFERENCES": ("Document", "ProcedureReference"),
    "DESCRIBES": ("Document", "ProcedureReference"),
    "HAS_FINDING": ("MaintenanceCase", "Finding"),
    "APPROVED_FOR": ("Part", "Configuration"),
    "DEFINES": ("Document", "AlarmType"),
}
# Most connections have one endpoint pair. Cases and events both refer to equipment.
EDGE_TYPES = {name: {pair} for name, pair in EDGE_TYPES.items()}
EDGE_TYPES["RECORDED_FOR"].add(("MaintenanceCase", "Equipment"))


class GraphBundle(Record):
    format_version: Literal[2] = 2
    # Serialization stays compatible; this is separate from the reviewed stock schema.
    ontology_version: int = Field(default=1, ge=1, le=3)
    mode: Literal["replay", "live"]
    nodes: list[Node]
    edges: list[Edge]
    held: list[dict] = Field(default_factory=list)
    extraction_trace: list[dict] = Field(default_factory=list)

    def validate_links(self):
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("Duplicate node identity.")
        for edge in self.edges:
            if edge.source not in by_id or edge.target not in by_id:
                raise ValueError("Relationship points to an unknown node.")
            actual = (by_id[edge.source].kind, by_id[edge.target].kind)
            if actual not in EDGE_TYPES[edge.relation]:
                raise ValueError("Relationship has invalid endpoint types.")
        for node in self.nodes:
            if node.kind == "Finding" and node.properties.get("status") not in {
                "reported",
                "suspected",
                "confirmed",
            }:
                raise ValueError("Finding needs a supported source-status label.")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()
