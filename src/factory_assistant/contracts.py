"""Typed decisions returned by the construction agents.

Every value an agent reads from a file is a Quote: the text as written plus the line
where it starts. Code verifies each quote before anything reaches the graph, so the
schemas describe what to point at, not facts to assert.
"""

from typing import Literal

from pydantic import Field

from factory_assistant.models import Record

DocType = Literal[
    "equipment_list",
    "event_log",
    "handover",
    "maintenance_history",
    "manual",
    "approved_parts",
    "stock_list",
    "policy_summary",
    "unsupported",
]


class Quote(Record):
    line: int = Field(description="Line number where the quoted text starts.")
    text: str = Field(
        description="Text copied exactly from the source. It may continue onto following lines."
    )


class Label(Record):
    line: int = Field(description="Line number where the value is written.")
    text: str = Field(
        description="Only the identifier, code, date or label as printed (e.g. 'CT-200', "
        "'B', 'WO-604', '07:45', '14 Oct 2026'), never the sentence around it."
    )


class Triage(Record):
    doc_type: DocType
    alternative: DocType | None = Field(description="Second most likely type, or null.")
    reason: str = Field(description="One sentence naming the content that decided the type.")


# equipment_list -------------------------------------------------------------------


class AssetItem(Record):
    id: Label = Field(description="The equipment identifier, e.g. a tag or 'Line 1'.")
    name: Quote | None = Field(description="A descriptive name written for it, or null.")
    aliases: list[Quote] = Field(description="Other names the source explicitly gives it.")
    model: Label | None = Field(description="Model stated for this equipment, or null.")
    configuration: Label | None = Field(description="Configuration label stated, or null.")
    part_of: str | None = Field(
        description="Identifier of the equipment this is stated to be a component of, or null."
    )
    part_of_lines: list[int] = Field(description="Lines that establish the membership.")


class FlowItem(Record):
    upstream: str = Field(description="Identifier of the equipment material comes from.")
    downstream: str = Field(description="Identifier of the equipment material goes to.")
    line: int


class EquipmentListing(Record):
    assets: list[AssetItem]
    flows: list[FlowItem]


# handover -------------------------------------------------------------------------


class HandoverEntry(Record):
    time: Label = Field(description="The clock time that applies, from the line it is written on.")
    occurred_at: str = Field(description="ISO date-time without offset, e.g. 2026-10-14T07:45:00.")
    text: Quote = Field(description="The statement itself.")
    kind: Literal["observation", "suspicion", "action"]
    equipment: Quote | None = Field(description="Equipment named in this statement, or null.")


class Handover(Record):
    date: Label = Field(description="The date the notes refer to.")
    scope: Quote | None = Field(description="Line, area or equipment the whole note covers.")
    entries: list[HandoverEntry]


# maintenance_history --------------------------------------------------------------


class MaintenanceCaseItem(Record):
    reference: Label | None = Field(description="Job or work-order number, or null.")
    date: Label = Field(description="The date of the work.")
    occurred_on: str = Field(description="That date as ISO yyyy-mm-dd.")
    equipment: Quote = Field(
        description="Only the words that refer to the equipment (ID, tag or name), however vague."
    )
    problem: Quote | None
    finding: Quote | None
    finding_status: Literal["reported", "suspected", "confirmed"] | None
    work: Quote | None
    outcome: Quote | None
    configuration: Label | None = Field(description="Configuration label at the time, or null.")


class MaintenanceRecords(Record):
    cases: list[MaintenanceCaseItem]


# manual ---------------------------------------------------------------------------


class Applicability(Record):
    model: Label
    configuration: Label


class AlarmDefinition(Record):
    code: Label = Field(description="The alarm/fault code as printed.")
    meaning: Quote = Field(description="The sentence that defines it.")


class Manual(Record):
    document_id: Label | None
    revision: Label | None
    issued: Label | None
    issued_on: str | None = Field(description="Issue date as ISO yyyy-mm-dd, or null.")
    archived: bool = Field(description="True only if the document says it is archived.")
    applies_to: list[Applicability]
    section_headings: list[Quote] = Field(description="Heading line of each section, in order.")
    alarm_definitions: list[AlarmDefinition]
    procedure_references: list[Label] = Field(description="IDs of site procedures cited.")


# approved_parts / policy_summary --------------------------------------------------


class PartApproval(Record):
    part_id: Label
    description: Quote | None
    model: Label = Field(description="Equipment model covered; may be stated once in a heading.")
    configuration: Label


class PartsList(Record):
    approvals: list[PartApproval]


class PolicySummary(Record):
    procedure_id: Label | None = Field(
        description="ID of the site procedure or policy this file summarises, often in its title."
    )
    full_procedure_included: bool = Field(description="True only if the detailed steps are here.")
    basis: Quote | None = Field(description="Sentence showing whether the steps are included.")


# stock_list -----------------------------------------------------------------------


class ColumnMapping(Record):
    column: str = Field(description="Header cell exactly as written.")
    field: str | None = Field(description="Core field name, a new snake_case name, or null.")
    new_field_type: Literal["string", "integer", "number", "boolean"] | None
    new_field_meaning: str | None = Field(description="Meaning of a new field, from the file.")


class StockTable(Record):
    header: Quote = Field(description="The table's header row.")
    delimiter: Literal["|", ",", ";", "tab", "spaces"]
    columns: list[ColumnMapping]
    counted_at: Quote | None = Field(description="Where the count time is stated, if no column.")
    counted_at_iso: str | None = Field(description="That time as ISO date-time, or null.")


EXTRACTION_CONTRACTS = {
    "equipment_list": EquipmentListing,
    "handover": Handover,
    "maintenance_history": MaintenanceRecords,
    "manual": Manual,
    "approved_parts": PartsList,
    "policy_summary": PolicySummary,
    "stock_list": StockTable,
}


# equipment reconciliation ---------------------------------------------------------


class MentionDecision(Record):
    mention_index: int
    decision: Literal["match", "ambiguous", "unresolved"]
    equipment_id: str | None
    reason: str


class Reconciliation(Record):
    decisions: list[MentionDecision]
