"""Link checked facts into one graph: equipment registry, identity resolution, publication.

Equipment is known only after every equipment file has been read, so linking happens
once for the whole folder. Code resolves exact IDs and recorded aliases; the
reconciliation agent handles what code cannot, and its matches stay labelled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from factory_assistant.evidence import document_node, not_after_registration, plant_time, squash
from factory_assistant.extract import compact, merge
from factory_assistant.facts import (
    ApprovalFact,
    AssetFact,
    CaseFact,
    EventFact,
    FlowFact,
    ManualFact,
    NoteFact,
    NoteScope,
    ProcedureFact,
    StockFact,
)
from factory_assistant.graph_models import Edge, Evidence, GraphBundle, Node
from factory_assistant.models import SourceSnapshot


@dataclass
class FileResult:
    source: SourceSnapshot
    doc_type: str | None
    method: str
    facts: list = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)


class GraphBuilder:
    """Accumulates nodes and links; repeated facts merge their evidence instead of duplicating."""

    def __init__(self, dataset: str):
        self.dataset = dataset
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple, Edge] = {}
        self.held: list[dict] = []
        self.trace: list[dict] = []

    def node(self, identity: str, kind: str, properties: dict, evidence: list[Evidence]) -> Node:
        properties = {k: v for k, v in properties.items() if v is not None}
        if identity in self.nodes:
            existing = self.nodes[identity]
            if existing.kind != kind:
                raise ValueError(f"{identity} is already a {existing.kind}.")
            existing.evidence = merge(existing.evidence, evidence)
            for key, value in properties.items():
                existing.properties.setdefault(key, value)
            return existing
        self.nodes[identity] = Node(
            id=identity, kind=kind, properties=properties, evidence=evidence
        )
        return self.nodes[identity]

    def edge(self, source: str, relation: str, target: str, evidence: list[Evidence]):
        key = (source, relation, target)
        if key in self.edges:
            self.edges[key].evidence = merge(self.edges[key].evidence, evidence)
        else:
            self.edges[key] = Edge(
                source=source, relation=relation, target=target, evidence=evidence
            )

    def hold(self, source_id: str, stage: str, reason: str, item: str | None = None):
        self.held.append(
            {
                "source": source_id,
                "stage": stage,
                **({"item": item} if item else {}),
                "reason": reason,
            }
        )

    def configuration(self, model: str, configuration: str, evidence: list[Evidence]) -> str:
        identity = f"{model}:{configuration}"
        self.node(
            identity,
            "Configuration",
            {"model": model, "configuration": configuration, "dataset": self.dataset},
            evidence,
        )
        return identity


# 1. Equipment registry ------------------------------------------------------------


def build_equipment(builder: GraphBuilder, results: list[FileResult]):
    """Merge equipment from every list and register; conflicting setups are held."""
    assets: dict[str, list[tuple[str, AssetFact]]] = {}
    flows = []
    for result in results:
        for fact in result.facts:
            if isinstance(fact, AssetFact):
                assets.setdefault(fact.id, []).append((result.source.source_id, fact))
            elif isinstance(fact, FlowFact):
                flows.append((result.source.source_id, fact))
    for identity, entries in assets.items():
        facts = [fact for _, fact in entries]
        name = next((f.name for f in facts if f.name), identity)
        # Every name the files give the equipment, including a descriptive one.
        aliases = sorted({a for f in facts for a in [*f.aliases, f.name] if a and a != identity})
        builder.node(
            identity,
            "Equipment",
            {"name": name, "aliases": aliases, "dataset": builder.dataset},
            merge(*(f.evidence for f in facts)),
        )
        setups = {(f.model, f.configuration) for f in facts if f.configuration}
        if len(setups) > 1:
            builder.hold(
                entries[0][0], "equipment", "Files disagree on its configuration.", identity
            )
        elif setups:
            model, configuration = setups.pop()
            refs = merge(*(f.evidence for f in facts if f.configuration))
            target = builder.configuration(model, configuration, refs)
            builder.edge(identity, "HAS_CONFIGURATION", target, refs)
    known = {compact(identity): identity for identity in assets}
    for identity, entries in assets.items():
        for source_id, fact in entries:
            if not fact.parent:
                continue
            parent = known.get(compact(fact.parent))
            if parent is None or parent == identity:
                builder.hold(
                    source_id, "equipment", f"Parent {fact.parent} is not known.", identity
                )
                continue
            # Cite the child's listing and the line where the parent is identified.
            parent_line = [
                r for r in builder.nodes[parent].evidence if compact(parent) in compact(r.excerpt)
            ][:1]
            builder.edge(
                parent, "HAS_COMPONENT", identity, merge(fact.parent_evidence, parent_line)
            )
    for source_id, flow in flows:
        upstream, downstream = (
            known.get(compact(flow.upstream)),
            known.get(compact(flow.downstream)),
        )
        if not upstream or not downstream or upstream == downstream:
            builder.hold(
                source_id,
                "equipment",
                "Flow names unknown equipment.",
                f"{flow.upstream} -> {flow.downstream}",
            )
            continue
        builder.edge(upstream, "SENDS_TO", downstream, flow.evidence)


# 2. Identity resolution -----------------------------------------------------------


class Resolver:
    """Exact IDs, names and aliases (ignoring case, spaces and hyphens) resolve in code."""

    def __init__(self, equipment: list[Node]):
        self.equipment = equipment
        self.ids = {n.id for n in equipment}

    def registry(self) -> list[dict]:
        return [
            {"id": n.id, "name": n.properties.get("name", n.id), "aliases": n.properties["aliases"]}
            for n in self.equipment
        ]

    def match(self, mention: str) -> tuple[str | None, str]:
        target = compact(mention)
        exact = {
            n.id
            for n in self.equipment
            if target
            in {
                compact(x)
                for x in [n.id, str(n.properties.get("name", "")), *n.properties["aliases"]]
            }
        }
        if len(exact) == 1:
            return exact.pop(), "exact ID, name or alias"
        if exact:
            return None, "several equipment records share this name"
        # Whole-word windows: 'packing outfeed CV01' contains the alias and the ID variant.
        words = re.findall(r"[0-9a-z]+", mention.casefold())
        windows = {
            "".join(words[i:j])
            for i in range(len(words))
            for j in range(i + 1, min(len(words), i + 6) + 1)
        }
        contained = {
            n.id
            for n in self.equipment
            if any(compact(x) in windows for x in [n.id, *n.properties["aliases"]] if compact(x))
        }
        if len(contained) == 1:
            return contained.pop(), "mention contains a recorded ID or alias"
        return None, "several candidates" if contained else "no recorded ID or alias"


def mentions_in(result: FileResult) -> dict[str, str]:
    """Each distinct mention with the source line it came from (context for the agent)."""
    found = {}
    for fact in result.facts:
        if isinstance(fact, (EventFact, NoteScope)):
            found.setdefault(fact.mention, fact.evidence[0].excerpt)
        elif isinstance(fact, NoteFact) and fact.mention:
            found.setdefault(fact.mention, fact.evidence[-1].excerpt)
        elif isinstance(fact, CaseFact):
            found.setdefault(fact.mention, fact.mention_evidence[0].excerpt)
    return found


def resolve_file(
    result: FileResult, resolver: Resolver, agents, failures: tuple = (ValueError,)
) -> dict[str, dict]:
    resolved, pending = {}, []
    for mention, line in mentions_in(result).items():
        identity, basis = resolver.match(mention)
        resolved[mention] = {"id": identity, "basis": basis, "by": "code"}
        if identity is None:
            pending.append({"mention_index": len(pending), "mention": mention, "source_line": line})
    if not pending:
        return resolved
    try:
        decision, meta = agents.reconcile(result.source, pending, resolver.registry())
    except failures as exc:
        # Exact matches made by code stand; the pending mentions stay unresolved.
        result.trace.append({"stage": "reconcile", "failed": str(exc)})
        for item in pending:
            resolved[item["mention"]]["basis"] = f"reconciliation unavailable: {exc}"
        return resolved
    result.trace.append({**meta, "stage": "reconcile", "decision": decision.model_dump()})
    for item in decision.decisions:
        if not 0 <= item.mention_index < len(pending):
            continue
        mention = pending[item.mention_index]["mention"]
        if item.decision == "match" and item.equipment_id in resolver.ids:
            resolved[mention] = {"id": item.equipment_id, "basis": item.reason, "by": "agent"}
        else:
            resolved[mention] = {
                "id": None,
                "basis": f"{item.decision}: {item.reason}",
                "by": "agent",
            }
    return resolved


# 3. Facts to graph ----------------------------------------------------------------


def add_document(builder: GraphBuilder, result: FileResult, extra: dict | None = None) -> str:
    document = document_node(result.source, result.doc_type, builder.dataset)
    document.properties.update({k: v for k, v in (extra or {}).items() if v is not None})
    builder.nodes[document.id] = document
    return document.id


def add_manual(
    builder: GraphBuilder, result: FileResult, fact: ManualFact, disputed: set[str]
) -> set[str]:
    first = fact.configurations[0] if fact.configurations else None
    sections = [s.model_dump() for s in fact.sections]
    document = add_document(
        builder,
        result,
        {
            "model": first.model if first else None,
            "configuration": first.configuration if first else None,
            "document_id": fact.document_id,
            "revision": fact.revision,
            "issued_on": fact.issued_on,
            "archived_copy": fact.archived,
            "sections_json": json.dumps(sections) if sections else None,
        },
    )
    for scope in fact.configurations:
        target = builder.configuration(scope.model, scope.configuration, scope.evidence)
        builder.edge(document, "APPLIES_TO", target, scope.evidence)
    for alarm in fact.alarms:
        if alarm.code in disputed:
            builder.hold(
                result.source.source_id,
                "manual",
                "Manuals define this alarm code differently; which meaning applies is unknown.",
                alarm.code,
            )
            continue
        identity = "alarm:" + alarm.code
        builder.node(
            identity, "AlarmType", {"code": alarm.code, "meaning": alarm.meaning}, alarm.evidence
        )
        builder.edge(document, "DEFINES", identity, alarm.evidence)
    for reference in fact.procedures:
        identity = "procedure:" + reference.procedure_id
        builder.node(
            identity,
            "ProcedureReference",
            {
                "procedure_id": reference.procedure_id,
                "details_available": False,
                "description": f"Site procedure {reference.procedure_id} (not in the folder)",
            },
            reference.evidence,
        )
        builder.edge(document, "REFERENCES", identity, reference.evidence)
    return {alarm.code for alarm in fact.alarms} - disputed


def link(builder, result, mention, resolutions, kind: str) -> tuple[str | None, str]:
    found = resolutions.get(mention) or {"id": None, "basis": "not resolved"}
    if found["id"] is None:
        builder.hold(
            result.source.source_id, "identity", f"Equipment {mention!r}: {found['basis']}", kind
        )
    return found["id"], "agent: " + found["basis"] if found.get("by") == "agent" else found["basis"]


def add_events(builder, results, resolutions, alarms: set[str]):
    """One controller event exported in several files is one event citing every file.

    An event is identified by its equipment and controller event ID. Copies that
    disagree are all held: we cannot tell which one is right.
    """
    copies: dict[tuple[str, str], list] = {}
    for result in results:
        sid = result.source.source_id
        for fact in result.facts:
            target, _ = link(builder, result, fact.mention, resolutions[sid], fact.event_id)
            if target is not None:
                copies.setdefault((target, fact.event_id), []).append((sid, fact))
    for (target, event_id), found in copies.items():
        if len({(f.occurred_at, f.code, f.message) for _, f in found}) > 1:
            for sid, _ in found:
                builder.hold(
                    sid, "parser", "Another file records this event differently.", event_id
                )
            continue
        first_sid, fact = found[0]
        refs = merge(*(f.evidence for _, f in found))
        identity = f"{first_sid}:{event_id}"
        builder.node(
            identity,
            "Event",
            {
                "event_id": event_id,
                "occurred_at": fact.occurred_at,
                "asset_id": target,
                "code": fact.code,
                "message": fact.message,
                "dataset": builder.dataset,
            },
            refs,
        )
        builder.edge(identity, "RECORDED_FOR", target, refs)
        if fact.code in alarms:
            builder.edge(identity, "REPORTS_ALARM", "alarm:" + fact.code, refs)


def add_handover(builder, result, resolutions):
    sid = result.source.source_id
    scope = next((f for f in result.facts if isinstance(f, NoteScope)), None)
    scope_id = resolutions.get(scope.mention, {}).get("id") if scope else None
    for fact in (f for f in result.facts if isinstance(f, NoteFact)):
        # A statement's own equipment wins; otherwise the note's line or area applies.
        own = resolutions.get(fact.mention, {}).get("id") if fact.mention else None
        target = own or scope_id
        if target is None:
            builder.hold(
                sid, "identity", "No equipment, line or area identified.", f"line {fact.line}"
            )
            continue
        identity = f"{sid}:note:{fact.line}"
        refs = merge(fact.evidence, scope.evidence if scope and not own else [])
        builder.node(
            identity,
            "Event",
            {
                "event_id": identity,
                "occurred_at": fact.occurred_at,
                "asset_id": target,
                "code": "UNVERIFIED_SUSPICION" if fact.kind == "suspicion" else "HANDOVER_NOTE",
                "message": fact.text,
                "observation_kind": fact.kind,
                "dataset": builder.dataset,
                "confirmed_diagnosis": False,
                "recorded_at": result.source.registered_at,
            },
            refs,
        )
        builder.edge(identity, "RECORDED_FOR", target, refs)


def add_cases(builder, result, resolutions):
    sid = result.source.source_id
    for index, fact in enumerate(f for f in result.facts if isinstance(f, CaseFact)):
        target, basis = link(builder, result, fact.mention, resolutions, fact.case_id)
        if target is None:
            continue
        identity = "case:" + (fact.case_id if fact.referenced else f"{sid}:{index + 1}")
        if identity in builder.nodes:
            builder.hold(
                sid, "identity", "Job reference already exists in another file.", fact.case_id
            )
            continue
        builder.node(
            identity,
            "MaintenanceCase",
            {
                "case_id": fact.case_id,
                "asset_id": target,
                "occurred_on": fact.occurred_on,
                "date_precision": "day",
                "recorded_at": result.source.registered_at,
                "dataset": builder.dataset,
                "reported_problem": fact.problem or "not recorded",
                "action": fact.work or "not recorded",
                "outcome": fact.outcome or "not recorded",
                "configuration_at_time": fact.configuration or "unknown",
                "additional_notes": [],
                "equipment_reference": fact.mention,
                "identity_basis": basis,
            },
            fact.evidence,
        )
        builder.edge(identity, "RECORDED_FOR", target, fact.mention_evidence)
        if fact.finding:
            finding = identity + ":finding"
            builder.node(
                finding,
                "Finding",
                {
                    "case_id": fact.case_id,
                    "text": fact.finding,
                    "status": fact.finding_status,
                    "status_basis": "Extraction agent's label, limited by the quoted wording",
                    "occurred_on": fact.occurred_on,
                    "recorded_at": result.source.registered_at,
                    "dataset": builder.dataset,
                },
                fact.finding_evidence,
            )
            builder.edge(identity, "HAS_FINDING", finding, fact.finding_evidence)


def add_parts(builder, result):
    for fact in (f for f in result.facts if isinstance(f, ApprovalFact)):
        identity = "part:" + fact.part_id
        builder.node(
            identity,
            "Part",
            {
                "part_id": fact.part_id,
                "description": fact.description,
                "dataset": builder.dataset,
                "recorded_at": result.source.registered_at,
            },
            fact.evidence,
        )
        target = builder.configuration(fact.model, fact.configuration, fact.evidence)
        builder.edge(identity, "APPROVED_FOR", target, fact.evidence)


def add_policy(builder, result, document: str):
    for fact in (f for f in result.facts if isinstance(f, ProcedureFact)):
        identity = "procedure:" + fact.procedure_id
        description = (
            "Site procedure "
            + fact.procedure_id
            + ("" if fact.details_available else " — summary only")
        )
        node = builder.node(
            identity,
            "ProcedureReference",
            {
                "procedure_id": fact.procedure_id,
                "details_available": fact.details_available,
                "description": description,
            },
            fact.evidence,
        )
        # The policy file is the authority on whether the full procedure is present.
        node.properties.update(details_available=fact.details_available, description=description)
        builder.edge(document, "DESCRIBES", identity, fact.evidence)


def coerce(value: str, data_type: str):
    if data_type == "integer":
        if not re.fullmatch(r"-?\d+", value):
            raise ValueError(f"{value!r} is not a whole number.")
        return int(value)
    if data_type == "number":
        return float(value)
    if data_type == "boolean":
        if value.lower() not in {"true", "false"}:
            raise ValueError(f"{value!r} is not true or false.")
        return value.lower() == "true"
    return value


def add_stock(builder, result, fact: StockFact, review, zone: ZoneInfo):
    """New columns wait for human review; known columns publish with their dated counts."""
    sid = result.source.source_id
    schema = review.schema()
    definitions = schema.by_name()
    waiting = [f for f in fact.proposed if f.name not in definitions]
    if waiting:
        mapping = ", ".join(f"{column} → {name}" for column, name in fact.mapping.items())
        proposal = review.submit(
            result.source, waiting, f"{fact.header_excerpt}\nColumn mapping: {mapping}"
        )
        builder.hold(
            sid,
            "schema review",
            f"New stock fields await review (proposal {proposal['id']}, {proposal['status']}).",
            ", ".join(f.name for f in waiting),
        )
        return
    # Rows for the same part, location and time all publish: the stock tool shows equal
    # counts once and reports differing ones as a conflict, never choosing between them.
    published = 0
    for row in fact.rows:
        line = row.evidence[-1].line
        try:
            if missing := [k for k in ("part_id", "location") if not row.values.get(k)]:
                raise ValueError(f"The row has no {' or '.join(missing)}.")
            values = {}
            for name, raw in row.values.items():
                value = coerce(raw, definitions[name].data_type)
                if name.startswith("quantity") and (type(value) is not int or value < 0):
                    raise ValueError(f"{name} must be a non-negative whole number.")
                values[name] = value
            counted = plant_time(values.get("counted_at", fact.counted_at or ""), zone)
            not_after_registration(counted, result.source)
            values["counted_at"] = counted.isoformat()
            on_hand, reserved = values.get("quantity_on_hand"), values.get("quantity_reserved")
            if on_hand is not None and reserved is not None and reserved > on_hand:
                raise ValueError("Reserved quantity exceeds the physical count.")
        except (ValueError, KeyError) as exc:
            builder.hold(sid, "stock", str(exc), f"line {line}")
            continue
        part = "part:" + values["part_id"]
        builder.node(
            part, "Part", {"part_id": values["part_id"], "dataset": builder.dataset}, row.evidence
        )
        identity = f"stock:{result.source.sha256[:16]}:{line}"
        builder.node(
            identity,
            "StockRecord",
            {
                **{f"field_{k}": v for k, v in values.items()},
                "schema_version": schema.version,
                "dataset": builder.dataset,
            },
            row.evidence,
        )
        builder.edge(identity, "STOCK_OF", part, row.evidence)
        published += 1
    if published:
        add_document(builder, result)


def assemble(builder, results, resolutions, review, zone) -> None:
    by_type = {}
    for result in results:
        by_type.setdefault(result.doc_type, []).append(result)
    for result in by_type.get("equipment_list", []):
        add_document(builder, result)
    for result in by_type.get("policy_summary", []):
        add_policy(builder, result, add_document(builder, result))
    # An alarm code is one node; if manuals give it different meanings (say, two models
    # reuse E01), no meaning is chosen and events link to none.
    meanings: dict[str, set[str]] = {}
    for result in by_type.get("manual", []):
        for fact in result.facts:
            for alarm in fact.alarms:
                meanings.setdefault(alarm.code, set()).add(squash(alarm.meaning).casefold())
    disputed = {code for code, texts in meanings.items() if len(texts) > 1}
    alarms = set()
    for result in by_type.get("manual", []):
        for fact in result.facts:
            alarms |= add_manual(builder, result, fact, disputed)
    for result in by_type.get("approved_parts", []):
        add_document(builder, result)
        add_parts(builder, result)
    for result in by_type.get("event_log", []):
        add_document(builder, result)
    add_events(builder, by_type.get("event_log", []), resolutions, alarms)
    for result in by_type.get("handover", []):
        add_document(builder, result)
        add_handover(builder, result, resolutions[result.source.source_id])
    for result in by_type.get("maintenance_history", []):
        add_document(builder, result)
        add_cases(builder, result, resolutions[result.source.source_id])
    for result in by_type.get("stock_list", []):
        for fact in result.facts:
            add_stock(builder, result, fact, review, zone)


def bundle_from(builder: GraphBuilder, mode: str) -> GraphBundle:
    bundle = GraphBundle(
        ontology_version=3,
        mode=mode,
        nodes=list(builder.nodes.values()),
        edges=list(builder.edges.values()),
        held=builder.held,
        extraction_trace=builder.trace,
    )
    bundle.validate_links()
    return bundle
