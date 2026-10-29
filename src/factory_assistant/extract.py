"""Turn an agent's extraction into checked facts, one item at a time.

A failed quote, date or label holds only its own item. The problems are returned so
the pipeline can give the agent one chance to correct them.
"""

from __future__ import annotations

import re
from collections import Counter
from zoneinfo import ZoneInfo

from factory_assistant.contracts import (
    EquipmentListing,
    Handover,
    MaintenanceRecords,
    Manual,
    PartsList,
    PolicySummary,
    StockTable,
)
from factory_assistant.evidence import (
    evidence_at,
    locate,
    not_after_registration,
    plant_time,
    quote_refs,
    quoted_text,
    squash,
    supported_date,
    supported_time,
)
from factory_assistant.facts import (
    AlarmRef,
    ApprovalFact,
    AssetFact,
    CaseFact,
    ConfigurationRef,
    FlowFact,
    ManualFact,
    NoteFact,
    NoteScope,
    ProcedureFact,
    ProcedureRef,
    Section,
    StockFact,
    StockRow,
)
from factory_assistant.graph_models import Evidence
from factory_assistant.models import FieldDefinition, SourceSnapshot, initial_schema
from factory_assistant.parsers import split_row

IDENTIFIER = re.compile(r"[A-Z0-9][A-Z0-9_.-]*")


def canonical_id(text: str) -> str:
    """'Line 1' and 'LINE1' are one identifier; hyphens and digits stay as written."""
    return re.sub(r"\s+", "", text).upper()


def compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def merge(*groups: list[Evidence]) -> list[Evidence]:
    unique = {(r.source_id, r.line): r for group in groups for r in group}
    return [unique[key] for key in sorted(unique)]


def optional(source, quote):
    if quote is None:
        return None, []
    refs, text = locate(source, quote)
    return text, refs


def label(source, quote, what: str):
    """A short printed value (model, configuration, ID, code); the sentence around it fails."""
    if quote is None:
        return None, []
    refs, text = locate(source, quote)
    value = text.strip().rstrip(".,;:")
    if not re.fullmatch(r"[A-Za-z0-9][\w./+-]{0,39}", value):
        tokens = [t for t in re.findall(r"[A-Za-z0-9][\w./+-]*", text) if re.search(r"\d", t)]
        options = f" (candidates in that text: {', '.join(tokens)})" if tokens else ""
        raise ValueError(
            f"{what} {text!r} must be only the {what} itself as printed{options}, "
            "or null if the file does not state one."
        )
    return value, refs


# Wording that turns a statement into a restriction or exclusion, not an approval or
# applicability. Quote checks only show the values are written in the file; this check
# looks at the statement they come from, for the relationships answers rely on.
RESTRICTION = re.compile(
    r"\b(not|never|cannot|can't|don't|isn't|aren't|except|excluding|excludes?|"
    r"prohibited|forbidden)\b|substitut|instead of",
    re.IGNORECASE,
)


def statement_issue(refs: list[Evidence], what: str) -> str | None:
    text = " ".join(r.excerpt for r in refs)
    if RESTRICTION.search(text):
        return f"the statement {text.strip()!r} is a restriction or exclusion, not {what}."
    return None


def convert_equipment(source: SourceSnapshot, decision: EquipmentListing):
    facts, problems = [], []
    for item in decision.assets:
        try:
            refs = quote_refs(source, item.id)
            identity = canonical_id(item.id.text)
            if not IDENTIFIER.fullmatch(identity):
                raise ValueError("is not an equipment identifier.")
            name, name_refs = optional(source, item.name)
            model, model_refs = label(source, item.model, "model")
            configuration, config_refs = label(source, item.configuration, "configuration")
            if configuration and not model:
                raise ValueError("has a configuration without a stated model.")
            aliases, alias_refs = [], []
            for alias in item.aliases:
                aliases.append(quoted_text(source, alias))
                alias_refs += quote_refs(source, alias)
            parent_refs = []
            if item.part_of:
                # Linking adds the parent's own line, so a block listing can be cited.
                parent_refs = evidence_at(source, item.part_of_lines)
            facts.append(
                AssetFact(
                    id=identity,
                    name=name,
                    aliases=aliases,
                    model=model,
                    configuration=configuration,
                    parent=canonical_id(item.part_of) if item.part_of else None,
                    evidence=merge(refs, name_refs, model_refs, config_refs, alias_refs),
                    parent_evidence=merge(refs, parent_refs),
                )
            )
        except ValueError as exc:
            problems.append(f"Asset {item.id.text!r} (line {item.id.line}): {exc}")
    for flow in decision.flows:
        try:
            refs = evidence_at(source, [flow.line])
            text = compact(refs[0].excerpt)
            if compact(flow.upstream) not in text or compact(flow.downstream) not in text:
                raise ValueError("the line does not name both pieces of equipment.")
            facts.append(
                FlowFact(
                    upstream=canonical_id(flow.upstream),
                    downstream=canonical_id(flow.downstream),
                    evidence=refs,
                )
            )
        except ValueError as exc:
            problems.append(f"Flow {flow.upstream} -> {flow.downstream}: {exc}")
    return facts, problems


def clock_line(source: SourceSnapshot, time, statement_line: int) -> list[Evidence]:
    """A continuation line inherits the latest time written at or above it."""
    lines = source.text.splitlines()
    for number in range(statement_line, max(0, statement_line - 6), -1):
        if squash(time.text) in squash(lines[number - 1]):
            return evidence_at(source, [number])
    raise ValueError(f"Time {time.text!r} is not written at or above line {statement_line}.")


def convert_handover(source: SourceSnapshot, decision: Handover, zone: ZoneInfo):
    facts, problems = [], []
    try:
        date_refs = quote_refs(source, decision.date)
    except ValueError as exc:
        return [], [f"Note date: {exc}"]
    if decision.scope:
        try:
            facts.append(
                NoteScope(
                    mention=quoted_text(source, decision.scope),
                    evidence=quote_refs(source, decision.scope),
                )
            )
        except ValueError as exc:
            problems.append(f"Note scope: {exc}")
    for entry in decision.entries:
        try:
            moment = plant_time(entry.occurred_at, zone)
            supported_date(moment.date().isoformat(), decision.date.text)
            text_refs = quote_refs(source, entry.text)
            time_refs = clock_line(source, entry.time, text_refs[0].line)
            supported_time(moment, entry.time.text)
            not_after_registration(moment, source)
            mention, _ = optional(source, entry.equipment)
            facts.append(
                NoteFact(
                    line=text_refs[0].line,
                    occurred_at=moment.isoformat(),
                    text=squash(entry.text.text),
                    kind=entry.kind,
                    mention=mention,
                    evidence=merge(date_refs, time_refs, text_refs),
                )
            )
        except ValueError as exc:
            problems.append(f"Entry {entry.text.text!r} (line {entry.text.line}): {exc}")
    return facts, problems


def finding_status(claimed: str | None, text: str) -> str:
    """The agent labels the finding; code only blocks an upgrade the wording cannot carry."""
    status = claimed or "reported"
    lowered = text.casefold()
    negated = re.search(r"\b(not|un|no)\b[^.;]*confirm|unconfirmed", lowered)
    if status == "confirmed" and ("confirm" not in lowered or negated):
        return "reported"
    return status


def case_start(case) -> int:
    return min(q.line for q in (case.reference, case.date) if q is not None)


def outside_section(lines: set[int], start: int, starts: list[int]) -> list[int]:
    """A job's section runs from its reference or date line to the next job's. Each quote
    can exist in the file and still belong to another job; lines before the first job (a
    heading shared by all) are allowed."""
    end = min((s for s in starts if s > start), default=None)
    return sorted(
        n for n in lines if (n < start and n >= starts[0]) or (end is not None and n >= end)
    )


def convert_cases(source: SourceSnapshot, decision: MaintenanceRecords):
    facts, problems = [], []
    starts = sorted({case_start(case) for case in decision.cases})
    references = Counter(
        squash(c.reference.text) for c in decision.cases if c.reference is not None
    )
    for index, case in enumerate(decision.cases):
        shown = case.reference.text if case.reference else f"case {index + 1}"
        try:
            reference, reference_refs = label(source, case.reference, "job reference")
            if reference and references[reference] > 1:
                raise ValueError("the same job reference appears twice; both are held.")
            date_refs = quote_refs(source, case.date)
            day = supported_date(case.occurred_on, case.date.text)
            not_after_registration(day, source)
            fields = {
                name: optional(source, getattr(case, name))
                for name in ("problem", "finding", "work", "outcome")
            }
            fields["configuration"] = label(source, case.configuration, "configuration")
            if fields["problem"][0] is None and fields["work"][0] is None:
                raise ValueError("needs a reported problem or recorded work.")
            finding, finding_refs = fields["finding"]
            equipment_refs = quote_refs(source, case.equipment)
            lines = {
                r.line
                for refs in (
                    reference_refs,
                    date_refs,
                    equipment_refs,
                    *(r for _, r in fields.values()),
                )
                for r in refs
            }
            if stray := outside_section(lines, case_start(case), starts):
                raise ValueError(
                    f"lines {', '.join(map(str, stray))} belong to another job's section; "
                    "every field must come from this job's own entry."
                )
            facts.append(
                CaseFact(
                    case_id=reference or f"{source.source_id} item {index + 1}",
                    referenced=reference is not None,
                    occurred_on=day.isoformat(),
                    mention=quoted_text(source, case.equipment),
                    mention_evidence=equipment_refs,
                    problem=fields["problem"][0],
                    work=fields["work"][0],
                    outcome=fields["outcome"][0],
                    configuration=fields["configuration"][0],
                    finding=finding,
                    finding_status=finding_status(case.finding_status, finding)
                    if finding
                    else None,
                    finding_evidence=merge(date_refs, finding_refs),
                    evidence=merge(
                        reference_refs,
                        date_refs,
                        equipment_refs,
                        *(refs for _, refs in fields.values()),
                    ),
                )
            )
        except ValueError as exc:
            problems.append(f"Case {shown!r}: {exc}")
    return facts, problems


def convert_manual(source: SourceSnapshot, decision: Manual):
    problems = []

    def checked(quote, what, short=True):
        if quote is None:
            return None, []
        try:
            return label(source, quote, what) if short else optional(source, quote)
        except ValueError as exc:
            problems.append(f"{what}: {exc}")
            return None, []

    document_id, _ = checked(decision.document_id, "document ID")
    revision_text, _ = checked(decision.revision, "revision")
    issued_text, _ = checked(decision.issued, "issue date", short=False)
    issued_on = None
    if issued_text and decision.issued_on:
        try:
            issued_on = supported_date(decision.issued_on, issued_text).isoformat()
        except ValueError as exc:
            problems.append(f"Issue date: {exc}")
    archived = decision.archived
    if archived and not re.search(r"archiv|supersed", source.text, re.I):
        problems.append("Archived: the document does not say it is archived.")
        archived = False
    configurations = []
    for scope in decision.applies_to:
        model, model_refs = checked(scope.model, "model")
        configuration, config_refs = checked(scope.configuration, "configuration")
        issue = statement_issue(config_refs, "applicability") if configuration else None
        if issue:
            problems.append(f"Applies to {model}:{configuration}: {issue}")
        elif model and configuration:
            configurations.append(
                ConfigurationRef(
                    model=model,
                    configuration=configuration,
                    evidence=merge(model_refs, config_refs),
                )
            )
    lines = source.text.splitlines()
    starts = []
    for heading in decision.section_headings:
        try:
            starts.append((quote_refs(source, heading)[0].line, squash(heading.text)))
        except ValueError as exc:
            problems.append(f"Section heading: {exc}")
    starts = sorted(set(starts))
    sections = []
    for index, (start, heading) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines) + 1
        numbers = [n for n in range(start, end) if lines[n - 1].strip()]
        refs = evidence_at(source, numbers)
        sections.append(
            Section(heading=heading, text="\n".join(r.excerpt for r in refs), evidence=refs)
        )
    alarms = []
    for alarm in decision.alarm_definitions:
        code, code_refs = checked(alarm.code, "alarm code")
        meaning, meaning_refs = checked(alarm.meaning, "alarm meaning", short=False)
        if code and not re.fullmatch(r"[A-Z][A-Z0-9_]+", code):
            problems.append(f"Alarm code {code!r}: not a printed code.")
        elif code and meaning:
            alarms.append(
                AlarmRef(code=code, meaning=meaning, evidence=merge(code_refs, meaning_refs))
            )
    procedures = []
    for reference in decision.procedure_references:
        identity, refs = checked(reference, "procedure ID")
        if identity:
            procedures.append(ProcedureRef(procedure_id=identity, evidence=refs))
    revision = re.search(r"\d+", revision_text) if revision_text else None
    fact = ManualFact(
        document_id=document_id,
        revision=int(revision[0]) if revision else None,
        issued_on=issued_on,
        archived=archived,
        configurations=configurations,
        sections=sections,
        alarms=alarms,
        procedures=procedures,
    )
    return [fact], problems


def convert_parts(source: SourceSnapshot, decision: PartsList):
    facts, problems = [], []
    for approval in decision.approvals:
        try:
            part_id, part_refs = label(source, approval.part_id, "part number")
            description, description_refs = optional(source, approval.description)
            model, model_refs = label(source, approval.model, "model")
            configuration, config_refs = label(source, approval.configuration, "configuration")
            # One approval is one statement: the part and its configuration on one line,
            # with no prohibition or non-substitution wording in it.
            if {r.line for r in part_refs} != {r.line for r in config_refs}:
                raise ValueError(
                    "the part and its configuration must come from the same statement."
                )
            issue = statement_issue(part_refs, "an approval")
            if issue:
                raise ValueError(issue)
            facts.append(
                ApprovalFact(
                    part_id=part_id,
                    description=description,
                    model=model,
                    configuration=configuration,
                    evidence=merge(
                        evidence_at(source, [1]),
                        part_refs,
                        description_refs,
                        model_refs,
                        config_refs,
                    ),
                )
            )
        except ValueError as exc:
            problems.append(f"Approval {approval.part_id.text!r}: {exc}")
    return facts, problems


def convert_policy(source: SourceSnapshot, decision: PolicySummary):
    if decision.procedure_id is None:
        return [], []
    try:
        identity, refs = label(source, decision.procedure_id, "procedure ID")
        basis = quote_refs(source, decision.basis) if decision.basis else []
        fact = ProcedureFact(
            procedure_id=identity,
            details_available=decision.full_procedure_included,
            evidence=merge(refs, basis),
        )
        return [fact], []
    except ValueError as exc:
        return [], [f"Procedure: {exc}"]


CORE_STOCK_FIELDS = {field.name for field in initial_schema().fields}


def convert_stock(source: SourceSnapshot, decision: StockTable, zone: ZoneInfo):
    """Code parses every row; the agent only explains the table's layout and columns."""
    header_refs = quote_refs(source, decision.header)
    lines = source.text.splitlines()
    header_line = header_refs[0].line
    headers = split_row(lines[header_line - 1], decision.delimiter)
    if [c.column.strip() for c in decision.columns] != headers:
        raise ValueError(f"Column list does not match the parsed header {headers}.")
    fields, proposed = [], []
    for column in decision.columns:
        name = column.field
        if name is None:
            fields.append("")
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name) or name in fields:
            raise ValueError(f"Field name {name!r} is invalid or repeated.")
        fields.append(name)
        if name not in CORE_STOCK_FIELDS:
            if not column.new_field_type or not column.new_field_meaning:
                raise ValueError(f"New field {name!r} needs a type and a meaning.")
            proposed.append(
                FieldDefinition(
                    name=name,
                    data_type=column.new_field_type,
                    description=column.new_field_meaning,
                )
            )
    if not {"part_id", "location"} <= set(fields):
        raise ValueError("A stock table needs part and location columns.")
    counted_at, counted_refs = None, []
    if "counted_at" not in fields:
        if not decision.counted_at or not decision.counted_at_iso:
            raise ValueError("No count time: neither a column nor a stated snapshot time.")
        counted_refs = quote_refs(source, decision.counted_at)
        moment = plant_time(decision.counted_at_iso, zone)
        supported_date(moment.date().isoformat(), decision.counted_at.text)
        supported_time(moment, decision.counted_at.text)
        counted_at = moment.isoformat()
    rows = []
    for number in range(header_line + 1, len(lines) + 1):
        line = lines[number - 1]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = split_row(line, decision.delimiter)
        if len(cells) != len(headers):
            continue  # Notes and footers around the table are not rows.
        values = {name: cell for name, cell in zip(fields, cells) if name and cell}
        rows.append(
            StockRow(values=values, evidence=merge(counted_refs, evidence_at(source, [number])))
        )
    if not rows:
        raise ValueError("No table rows match the header.")
    return [
        StockFact(
            mapping={h: f or "(not stored)" for h, f in zip(headers, fields)},
            fields=[f for f in fields if f],
            proposed=proposed,
            rows=rows,
            counted_at=counted_at,
            header_excerpt=lines[header_line - 1],
        )
    ], []


TAG = re.compile(r"(?<![\w-])[A-Z]{1,5}-\d{1,4}[A-Z]?(?![\w-])")


def coverage_hints(doc_type: str, source: SourceSnapshot, decision) -> list[str]:
    """Tag-like identifiers the equipment extraction neither used nor dismissed.

    These are hints for the agent's correction, not failures: a tag may be a model or
    a part number, and the agent decides. Nothing is added to the graph from them.
    """
    if doc_type != "equipment_list":
        return []
    used = {compact(a.id.text) for a in decision.assets}
    used |= {compact(q.text) for a in decision.assets for q in (a.model, a.configuration) if q}
    hints = []
    for number, line in enumerate(source.text.splitlines(), 1):
        for tag in TAG.findall(line):
            if compact(tag) not in used:
                used.add(compact(tag))
                hints.append(
                    f"{tag} (line {number}) is not in the extraction; add it if it is equipment."
                )
    return hints


def substantive(facts: list) -> bool:
    return any(not isinstance(fact, NoteScope) for fact in facts)


def convert(doc_type: str, source: SourceSnapshot, decision, zone: ZoneInfo):
    if doc_type == "equipment_list":
        return convert_equipment(source, decision)
    if doc_type == "handover":
        return convert_handover(source, decision, zone)
    if doc_type == "maintenance_history":
        return convert_cases(source, decision)
    if doc_type == "manual":
        return convert_manual(source, decision)
    if doc_type == "approved_parts":
        return convert_parts(source, decision)
    if doc_type == "policy_summary":
        return convert_policy(source, decision)
    if doc_type == "stock_list":
        try:
            return convert_stock(source, decision, zone)
        except ValueError as exc:
            return [], [f"Stock table: {exc}"]
    raise ValueError(f"No converter for {doc_type}.")
