"""Facts that passed their source checks but are not yet linked into the graph.

Each fact carries exact evidence. Equipment references are still plain mentions here;
identity resolution happens once the whole folder's equipment registry is known.
"""

from factory_assistant.graph_models import Evidence
from factory_assistant.models import FieldDefinition, Record


class AssetFact(Record):
    id: str
    name: str | None
    aliases: list[str]
    model: str | None
    configuration: str | None
    parent: str | None
    evidence: list[Evidence]
    parent_evidence: list[Evidence]


class FlowFact(Record):
    upstream: str
    downstream: str
    evidence: list[Evidence]


class EventFact(Record):
    event_id: str
    occurred_at: str
    mention: str
    code: str
    message: str
    evidence: list[Evidence]


class NoteScope(Record):
    mention: str
    evidence: list[Evidence]


class NoteFact(Record):
    line: int
    occurred_at: str
    text: str
    kind: str
    mention: str | None
    evidence: list[Evidence]


class CaseFact(Record):
    case_id: str
    referenced: bool
    occurred_on: str
    mention: str
    mention_evidence: list[Evidence]
    problem: str | None
    work: str | None
    outcome: str | None
    configuration: str | None
    finding: str | None
    finding_status: str | None
    finding_evidence: list[Evidence]
    evidence: list[Evidence]


class ConfigurationRef(Record):
    model: str
    configuration: str
    evidence: list[Evidence]


class Section(Record):
    heading: str
    text: str
    evidence: list[Evidence]


class AlarmRef(Record):
    code: str
    meaning: str
    evidence: list[Evidence]


class ProcedureRef(Record):
    procedure_id: str
    evidence: list[Evidence]


class ManualFact(Record):
    document_id: str | None
    revision: int | None
    issued_on: str | None
    archived: bool
    configurations: list[ConfigurationRef]
    sections: list[Section]
    alarms: list[AlarmRef]
    procedures: list[ProcedureRef]


class ApprovalFact(Record):
    part_id: str
    description: str | None
    model: str
    configuration: str
    evidence: list[Evidence]


class ProcedureFact(Record):
    procedure_id: str
    details_available: bool
    evidence: list[Evidence]


class StockRow(Record):
    values: dict[str, str]
    evidence: list[Evidence]


class StockFact(Record):
    mapping: dict[str, str]
    fields: list[str]
    proposed: list[FieldDefinition]
    rows: list[StockRow]
    counted_at: str | None
    header_excerpt: str
