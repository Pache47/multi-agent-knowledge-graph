"""Bounded, read-only tools for questions over any validated snapshot.

The model selects tools and their arguments. Python owns access filtering, graph
walking, aggregation and citation identity. No tool accepts generated SQL/Cypher,
file paths or write commands. Tool docstrings become the model-facing descriptions.
"""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import Field

from factory_assistant.answer_models import EvidenceCard
from factory_assistant.assistant_contracts import ComputedFact
from factory_assistant.graph_models import EDGE_TYPES, Evidence, GraphBundle
from factory_assistant.models import Record
from factory_assistant.passage_search import EmbeddingCache, search_passages, tokens


def timestamp(value: str) -> datetime:
    """Reject ambiguous local times instead of silently choosing a timezone."""
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Provide a timestamp with its timezone.")
    return parsed


class EquipmentQuery(Record):
    query: str = Field(min_length=1, max_length=200, description="Equipment ID, name or alias.")


class WalkQuery(Record):
    start_id: str = Field(description="node_id returned by a tool, not its citation ID.")
    relations: list[str] = Field(max_length=5, description="Allowed relationship names to follow.")
    direction: Literal["outgoing", "incoming", "both"]
    hops: int = Field(ge=1, le=3, description="Maximum graph distance, from one to three links.")


class EventQuery(Record):
    equipment_id: str = Field(description="A uniquely identified equipment ID.")
    event_code: str | None = Field(description="Exact code from records, or null for all codes.")
    since: str | None = Field(
        description="Inclusive timezone-aware start time, or null for all available records."
    )


class PassageQuery(Record):
    query: str = Field(
        min_length=1, max_length=600, description="Natural-language search or exact terms."
    )
    equipment_id: str | None = Field(
        description="Resolved equipment ID; null searches only site policy summaries."
    )
    limit: int = Field(ge=1, le=6)


class DocumentQuery(Record):
    query: str = Field(
        min_length=1, max_length=600, description="Natural-language search or exact terms."
    )
    limit: int = Field(ge=1, le=6)


class FinishQuery(Record):
    outcome: Literal["answer", "clarify", "insufficient_evidence"]
    question: str = Field(
        max_length=400,
        description="Neutral question only for clarify; empty for answer or insufficient_evidence.",
    )


class FinishRecords(Record):
    fact_ids: list[str] = Field(
        min_length=1,
        max_length=4,
        description="Exact computed fact or record_summary_id values returned by tools. "
        "Select only records that answer the current question completely.",
    )


class EquipmentScope(Record):
    equipment_id: str = Field(description="A uniquely resolved equipment ID from find_equipment.")


class GraphTools:
    """One request's visible graph and evidence ledger, isolated from other datasets."""

    def __init__(
        self,
        bundle: GraphBundle,
        dataset: str,
        as_of: str,
        user_context: str,
        embeddings: EmbeddingCache | None = None,
    ):
        bundle.validate_links()
        self.bundle, self.dataset, self.as_of = bundle, dataset, as_of
        self.cutoff = timestamp(as_of)
        self.user_context = user_context
        self.embeddings = embeddings
        self.documents = {
            n.properties["source_id"]: n
            for n in bundle.nodes
            if n.kind == "Document"
            and n.properties.get("dataset") == dataset
            and timestamp(n.properties["recorded_at"]) <= self.cutoff
        }
        # A fact stays visible with the evidence known at the cutoff: a later file that
        # also mentions it (a newer stock count of an approved part) must not hide it.
        visible = {
            n.id: n.model_copy(update={"evidence": refs})
            for n in bundle.nodes
            if (refs := self.known(n.evidence))
        }
        self.nodes = {
            n.id: n
            for n in visible.values()
            if n.properties.get("dataset", dataset) == dataset
            and (n.kind != "Event" or timestamp(n.properties["occurred_at"]) <= self.cutoff)
            and (
                n.kind != "StockRecord"
                or timestamp(n.properties["field_counted_at"]) <= self.cutoff
            )
            and (
                n.kind not in {"MaintenanceCase", "Finding"}
                or n.properties["occurred_on"]
                <= self.cutoff.astimezone(timestamp(n.properties["recorded_at"]).tzinfo)
                .date()
                .isoformat()
            )
        }
        self.edges = [
            e.model_copy(update={"evidence": refs})
            for e in bundle.edges
            if e.source in self.nodes
            and e.target in self.nodes
            and (refs := self.known(e.evidence))
        ]
        self.cards: dict[str, EvidenceCard] = {}
        self.facts: dict[str, ComputedFact] = {}
        self.resolved: set[str] = set()
        self.discovered: set[str] = set()
        # Tool coverage and warnings are context too. Previously only citation cards
        # reached drafting, losing explicit historical-configuration comparisons.
        self.observations: list[dict] = []

    def known(self, refs: list[Evidence]) -> list[Evidence]:
        """The citations from files registered by the cutoff. Any of them that no longer
        matches its file rejects the whole fact; none at all means it was not yet known."""
        current = [r for r in refs if r.source_id in self.documents]
        return current if current and self.available(current) else []

    def available(self, refs: list[Evidence]) -> bool:
        """A citation must match an accessible source snapshot, not merely have an ID."""
        for ref in refs:
            document = self.documents.get(ref.source_id)
            if document is None:
                return False
            p = document.properties
            lines = p["text"].splitlines()
            if (
                ref.sha256 != p["sha256"]
                or ref.path != p["path"]
                or ref.line > len(lines)
                or ref.excerpt != lines[ref.line - 1]
                or (
                    p.get("page_numbers_json")
                    and ref.page != json.loads(p["page_numbers_json"])[ref.line - 1]
                )
            ):
                return False
        return bool(refs)

    def remember(self, card: EvidenceCard) -> dict:
        """Register only evidence returned by a tool; final citations use this ledger."""
        self.cards[card.id] = card
        return card.model_dump()

    def node_card(self, identity: str) -> dict:
        node = self.nodes[identity]
        self.discovered.add(identity)
        properties = {
            k: v
            for k, v in node.properties.items()
            if node.kind != "Document" or k not in {"text", "sections_json", "page_numbers_json"}
        }
        card = self.remember(
            EvidenceCard(
                id="node:" + identity,
                title=str(properties.get("name", identity)),
                text=json.dumps({"id": identity, "kind": node.kind, "properties": properties}),
                evidence=node.evidence,
            )
        )
        return {"node_id": identity, **card}

    def find_equipment(self, query: str) -> dict:
        """Find equipment by ID/name/alias. Exact, unique names in the user's context resolve;
        partial matches are candidates only. If several candidates remain, ask which one.
        Never infer a replacement or a diagnosis from a name match.
        """
        exact, candidates = [], []
        for node in self.nodes.values():
            if node.kind != "Equipment":
                continue
            names = [
                node.id,
                str(node.properties.get("name", node.id)),
                *node.properties.get("aliases", []),
            ]
            if query.casefold().strip() in {name.casefold() for name in names}:
                exact.append(node.id)
            if any(tokens(query) & tokens(name) for name in names):
                candidates.append(node.id)
        # Models sometimes pass a phrase such as 'carton buffer BF-10'. A unique
        # explicit ID inside that phrase is stronger than overlapping words. It must
        # still occur in the technician's context below; a model cannot invent it.
        if not exact:
            exact = [
                n.id
                for n in self.nodes.values()
                if n.kind == "Equipment"
                and re.search(r"(?<![\w-])" + re.escape(n.id) + r"(?![\w-])", query, re.I)
            ]
        matches = exact or candidates
        confirmed = False
        if len(exact) == 1:
            node = self.nodes[exact[0]]
            names = [
                node.id,
                str(node.properties.get("name", node.id)),
                *node.properties.get("aliases", []),
            ]
            confirmed = any(
                re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", self.user_context, re.I)
                for name in names
            )
            if confirmed:
                self.resolved.add(exact[0])
        return {
            "resolved": confirmed,
            "matches": [self.node_card(i) for i in matches[:8]],
            "truncated": len(matches) > 8,
            "note": (
                "Use node_id (not the citation id) in tools. Candidates alone do not establish "
                "which machine is meant. query_events includes components automatically. "
                "Search manuals using the resolved machine ID. For explicit applicability "
                "paths walk HAS_CONFIGURATION and APPLIES_TO in both directions, two hops."
            ),
        }

    def walk_graph(self, start_id: str, relations: list[str], direction: str, hops: int) -> dict:
        """Follow selected graph links up to three hops from a previously returned node.
        Use configuration -> incoming APPLIES_TO to find matching manuals, or equipment ->
        HAS_COMPONENT for components. Returns paths and their source citations. A connection
        does not prove causation. A truncated result is not a complete neighbourhood.
        """
        if start_id not in self.discovered or not set(relations) <= EDGE_TYPES.keys():
            raise ValueError("Use a returned node ID and known relationship names.")
        if self.nodes[start_id].kind == "Equipment" and start_id not in self.resolved:
            # Components reached from an already resolved machine may be inspected too.
            if start_id not in self.component_ids():
                raise ValueError("Clarify which equipment before following its connections.")
        seen, frontier, paths = {start_id}, [(start_id, [])], []
        for _ in range(hops):
            following = []
            for current, path in frontier:
                for edge in self.edges:
                    if edge.relation not in relations:
                        continue
                    target = None
                    if direction in {"outgoing", "both"} and edge.source == current:
                        target = edge.target
                    elif direction in {"incoming", "both"} and edge.target == current:
                        target = edge.source
                    if target is None or target in seen:
                        continue
                    step = {"source": edge.source, "relation": edge.relation, "target": edge.target}
                    key = (
                        "link:"
                        + hashlib.sha256(json.dumps(step, sort_keys=True).encode()).hexdigest()[:16]
                    )
                    self.remember(
                        EvidenceCard(
                            id=key,
                            title="Graph connection",
                            text=json.dumps(step),
                            evidence=edge.evidence,
                        )
                    )
                    route = [*path, {**step, "citation": key}]
                    # A multi-hop conclusion needs the whole path, not just its last node.
                    # Return a citable path card as well as individual links for inspection.
                    path_key = (
                        "path:"
                        + hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()[
                            :16
                        ]
                    )
                    self.remember(
                        EvidenceCard(
                            id=path_key,
                            title="Connected evidence path",
                            text=json.dumps(route),
                            evidence=[
                                ref
                                for item in route
                                for ref in self.cards[item["citation"]].evidence
                            ],
                        )
                    )
                    paths.append(
                        {"node": self.node_card(target), "path": route, "path_citation": path_key}
                    )
                    seen.add(target)
                    following.append((target, route))
                    if len(paths) >= 30:
                        return {"paths": paths, "truncated": True}
            frontier = following
        return {"paths": paths, "truncated": False}

    def component_ids(self, roots: set[str] | None = None) -> set[str]:
        """Expand component ownership, not downstream material flow, with cycle protection."""
        seen = set(self.resolved if roots is None else roots)
        for _ in range(3):
            children = {
                e.target for e in self.edges if e.source in seen and e.relation == "HAS_COMPONENT"
            }
            if children <= seen:
                break
            seen |= children
        return seen

    def query_events(self, equipment_id: str, event_code: str | None, since: str | None) -> dict:
        """Retrieve dated events on a resolved machine and its components. Null code means
        all codes; an exact code filters them. Counts use unique graph event IDs in code.
        Zero means zero matching AVAILABLE records, never proof nothing happened. Without
        related log evidence, return unknown rather than zero. No live status is available.
        """
        if equipment_id not in self.component_ids() or equipment_id not in self.discovered:
            raise ValueError("Resolve one equipment name first.")
        start = timestamp(since) if since else None
        if start and start > self.cutoff:
            raise ValueError("Start time exceeds the question time.")
        related = self.component_ids({equipment_id})
        ids = {e.source for e in self.edges if e.relation == "RECORDED_FOR" and e.target in related}
        candidates = [self.nodes[i] for i in ids if self.nodes[i].kind == "Event"]
        # Handover observations are not controller occurrences; preserve that distinction.
        logs = [
            n
            for n in candidates
            if n.properties["code"] not in {"HANDOVER_NOTE", "UNVERIFIED_SUSPICION"}
        ]
        if not logs:
            return {"coverage": "no_related_log_evidence", "count": None, "events": []}
        # Treat spaces/case as formatting only when they match one recorded code.
        # This prevents 'motor overload' silently counting zero for MOTOR_OVERLOAD,
        # without inventing synonyms or conflating different alarms.
        supplied_code = event_code
        if event_code is not None:
            normalized = re.sub(r"[\W_]+", "_", event_code.casefold()).strip("_")
            alternatives = {
                n.properties["code"]
                for n in logs
                if re.sub(r"[\W_]+", "_", n.properties["code"].casefold()).strip("_") == normalized
            }
            if len(alternatives) == 1:
                event_code = alternatives.pop()
            recorded = {
                n.properties["code"]
                for n in self.nodes.values()
                if n.kind == "Event"
                and n.properties["code"] not in {"HANDOVER_NOTE", "UNVERIFIED_SUSPICION"}
            }
            if event_code not in recorded:
                # An unknown code is not a count of zero: say which codes exist instead.
                here = sorted({n.properties["code"] for n in logs})
                raise ValueError(
                    f"No event code {supplied_code!r} exists in the records. Codes recorded for "
                    f"this equipment: {', '.join(here)}. Use one of them or null for all."
                )
        matching = sorted(
            (
                n
                for n in logs
                if (event_code is None or n.properties["code"] == event_code)
                and (start is None or timestamp(n.properties["occurred_at"]) >= start)
            ),
            key=lambda n: n.properties["occurred_at"],
        )
        arguments = {
            "equipment": equipment_id,
            "event_code": event_code,
            "since": since,
            "as_of": self.as_of,
        }
        key = (
            "events:"
            + hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:16]
        )
        summary = {
            **arguments,
            "count": len(matching),
            "by_code": dict(Counter(n.properties["code"] for n in matching)),
            "coverage": "available_records_only",
        }
        refs = [r for n in (matching or logs) for r in n.evidence]
        card = EvidenceCard(
            id=key,
            title="Count from available event records",
            text=json.dumps(summary),
            evidence=refs,
        )
        self.remember(card)
        # A start before the first available record does not imply centuries of coverage.
        earliest_record = min(timestamp(n.properties["occurred_at"]) for n in logs)
        display_start = since if start and start > earliest_record else "the available history"
        fact = ComputedFact(
            id=key,
            text=(
                f"{equipment_id}: {len(matching)} matching recorded events "
                f"({event_code or 'all codes'}), from {display_start} "
                f"through {self.as_of}. Duplicate exports count once. This is not live status."
            ),
            citations=[key],
        )
        self.facts[key] = fact
        return {
            **summary,
            "supplied_code": supplied_code,
            "computed_fact": fact.model_dump(),
            "events": [self.node_card(n.id) for n in matching[:20]],
            "truncated": len(matching) > 20,
        }

    def passages(self, equipment_id: str | None) -> list[EvidenceCard]:
        """Select documents before ranking: inaccessible or inapplicable text is never embedded."""
        if equipment_id is not None and equipment_id not in self.resolved:
            raise ValueError(
                "Use a resolved machine for its documents. Resolved IDs: "
                + ", ".join(sorted(self.resolved))
                + ". A component may share its parent's guidance."
            )
        configurations = {
            e.target
            for e in self.edges
            if e.source == equipment_id and e.relation == "HAS_CONFIGURATION"
        }
        manuals = {
            e.source
            for e in self.edges
            if e.relation == "APPLIES_TO" and e.target in configurations
        }
        result = []
        for document in self.documents.values():
            p = document.properties
            role = p.get("role")
            if role == "stock_list":
                # Stock uses a scoped tool: paragraph search could surface superseded
                # or conflicting counts without the snapshot-selection rules.
                continue
            if role == "manual" and (len(configurations) != 1 or document.id not in manuals):
                continue
            if equipment_id is None and role != "policy_summary":
                continue
            if document.id not in self.nodes:
                continue
            if role == "maintenance_history":
                # Only accepted, equipment-scoped case blocks are searchable. Never fall
                # back to the complete file: it may contain held cases or other machines.
                related = self.component_ids({equipment_id}) if equipment_id else set()
                chunks = [
                    ("Maintenance " + n.properties["case_id"], n.evidence)
                    for n in self.nodes.values()
                    if n.kind == "MaintenanceCase"
                    and n.properties["asset_id"] in related
                    and all(ref.source_id == p["source_id"] for ref in n.evidence)
                ]
            elif "sections_json" in p:
                chunks = [
                    (s["heading"], [Evidence.model_validate(r) for r in s["evidence"]])
                    for s in json.loads(p["sections_json"])
                ]
            else:
                chunks = windows(p)
            for title, refs in chunks:
                if not self.available(refs):
                    continue
                key = f"passage:{p['source_id']}:{p['sha256'][:12]}:{refs[0].line}"
                result.append(
                    EvidenceCard(
                        id=key,
                        title=str(title),
                        text="\n".join(r.excerpt for r in refs),
                        evidence=refs,
                    )
                )
        return result

    def search_documents(self, query: str, limit: int) -> dict:
        """Fallback: search the source text of every file available at the question time,
        for what the other tools do not cover (a note in a stores file, a remark in a shift
        note). Use graph tools first. These passages are not linked to a machine and not
        checked by the graph: keep the scope and certainty they state, and never apply one
        machine's record to another. Manuals and stock numbers are excluded; use the graph
        tools for those. A passage from a file whose machine reference is unresolved comes
        labelled as a held record: it exists, but which machine it is about is unknown.
        """
        unresolved = {}
        for h in self.bundle.held:
            if h["stage"] == "identity":
                unresolved.setdefault(h["source"], []).append(f"{h.get('item', '')}: {h['reason']}")
        cards = []
        for document in self.documents.values():
            p = document.properties
            if document.id not in self.nodes or p.get("role") == "manual":
                continue
            held = unresolved.get(p["source_id"])
            for _, refs in windows(p):
                if p.get("role") == "stock_list":
                    refs = [r for r in refs if not re.search(r"\d", r.excerpt)]
                if not refs or not self.available(refs):
                    continue
                text = "\n".join(r.excerpt for r in refs)
                if held:
                    # Say what is unknown instead of leaving a gap that nearby records fill.
                    text = (
                        "HELD RECORD: the machine it refers to is not identified ("
                        + "; ".join(held)
                        + "). It exists, but must not be assigned to any machine.\n"
                        + text
                    )
                cards.append(
                    EvidenceCard(
                        id=f"source:{p['source_id']}:{p['sha256'][:12]}:{refs[0].line}",
                        title=("Held record · " if held else "Source text · ")
                        + p["path"].rsplit("/", 1)[-1],
                        text=text,
                        evidence=refs,
                    )
                )
        results = search_passages(query, cards, limit, self.embeddings)
        for result in results:
            self.remember(EvidenceCard.model_validate(result["card"]))
        return {
            "mode": "hybrid" if self.embeddings else "keyword",
            "results": results,
            "coverage": "source_text_fallback",
            "note": "Source text, not linked to a machine by the graph. Keep its stated scope; "
            "search ranking is not proof.",
        }

    def query_history(self, equipment_id: str) -> dict:
        """Read up to ten dated maintenance cases for resolved equipment and its components.
        Use for previous incidents, recorded work, findings and outcomes. Retain cases from
        older configurations but label the difference; unknown historical configuration
        stays unknown. 'Confirmed' means explicitly confirmed in that source, never today's
        diagnosis. A successful trial does not prove a permanent fix. No matching cases
        means no available evidence, not that maintenance never happened.
        """
        if equipment_id not in self.resolved:
            raise ValueError("Resolve the equipment before reading its history.")
        related = self.component_ids({equipment_id})
        ids = {e.source for e in self.edges if e.relation == "RECORDED_FOR" and e.target in related}
        cases = sorted(
            (self.nodes[i] for i in ids if self.nodes[i].kind == "MaintenanceCase"),
            key=lambda n: (n.properties["occurred_on"], n.id),
            reverse=True,
        )
        configurations = [
            self.nodes[e.target]
            for e in self.edges
            if e.source == equipment_id and e.relation == "HAS_CONFIGURATION"
        ]
        records = []
        for case in cases[:10]:
            historical = case.properties["configuration_at_time"]
            comparison = "unknown"
            # A component's historical setup is not necessarily the parent machine's
            # setup. Compare labels only for a case recorded on this exact equipment.
            if (
                historical != "unknown"
                and len(configurations) == 1
                and case.properties["asset_id"] == equipment_id
            ):
                comparison = (
                    "same_label_only"
                    if historical == configurations[0].properties["configuration"]
                    else "different_recorded_configuration"
                )
            findings = [
                self.node_card(e.target)
                for e in self.edges
                if e.source == case.id and e.relation == "HAS_FINDING"
            ]
            records.append(
                {
                    "case": self.node_card(case.id),
                    "findings": findings,
                    "configuration_comparison": comparison,
                }
            )
            # Preserve reported/confirmed status and trial-limited outcomes in code.
            # The model selects these summaries but cannot strengthen their wording.
            p = case.properties
            finding_text = (
                "; ".join(
                    f"{self.nodes[e.target].properties['status']} in the source: "
                    f"{self.nodes[e.target].properties['text']}"
                    for e in self.edges
                    if e.source == case.id and e.relation == "HAS_FINDING"
                )
                or "not recorded"
            )
            current = (
                str(configurations[0].properties["configuration"])
                if len(configurations) == 1
                else "unknown"
            )
            scope = f"Setup recorded then: {historical}. Current equipment setup: {current}."
            if comparison == "different_recorded_configuration":
                scope += " These recorded setups differ."
            elif comparison == "same_label_only":
                scope += " Matching labels alone do not prove an unchanged machine."
            text = (
                f"{p['case_id']} · {p['occurred_on']} · {p['asset_id']}\n"
                f"Reported problem: {p['reported_problem']}\nFinding — {finding_text}\n"
                f"Recorded work: {p['action']}\nRecorded outcome: {p['outcome']}\n{scope}"
            )
            fact = ComputedFact(
                id="history:" + case.id,
                text=text,
                kind="maintenance_record",
                citations=[
                    "node:" + case.id,
                    *[f["id"] for f in findings],
                    *["node:" + n.id for n in configurations],
                ],
            )
            self.facts[fact.id] = fact
            records[-1]["record_summary_id"] = fact.id
        return {
            "equipment": self.node_card(equipment_id),
            "current_configurations": [self.node_card(n.id) for n in configurations],
            "cases": records,
            "truncated": len(cases) > 10,
            "coverage": "available_records_only" if cases else "no_related_history_evidence",
            "note": "Past work is not a recommendation for today. "
            "Unknown finding/configuration stays unknown.",
        }

    def approved_parts(self, equipment_id: str) -> dict:
        """Find explicitly approved parts through equipment -> configuration <- part links.
        Requires exactly one current configuration. Returns cited two-hop paths and the
        parts-list limitations. Similar names or quantities do not prove compatibility.
        An absent approval is unknown, not proof of incompatibility; a prohibition needs
        explicit source evidence. Approval does not establish stock or permission to fit.
        """
        if equipment_id not in self.resolved:
            raise ValueError("Resolve the equipment before checking approved parts.")
        configurations = {
            e.target
            for e in self.edges
            if e.source == equipment_id and e.relation == "HAS_CONFIGURATION"
        }
        if len(configurations) != 1:
            return {
                "coverage": "configuration_unresolved",
                "parts": [],
                "note": "A single documented installed configuration is required.",
            }
        walked = self.walk_graph(equipment_id, ["HAS_CONFIGURATION", "APPROVED_FOR"], "both", 2)
        parts = [p for p in walked["paths"] if self.nodes[p["node"]["node_id"]].kind == "Part"]
        source_ids = {
            ref.source_id for p in parts for ref in self.nodes[p["node"]["node_id"]].evidence
        }
        boundaries = []
        for document in self.documents.values():
            if (
                document.properties.get("role") == "approved_parts"
                and document.properties["source_id"] in source_ids
            ):
                # The small approved list contains explicit non-substitution and scope
                # statements. Return them with the matched paths, not as a stock claim.
                for card in self.passages(equipment_id):
                    if card.evidence[0].source_id == document.properties["source_id"]:
                        boundaries.append(self.remember(card))
        return {
            "equipment": self.node_card(equipment_id),
            "parts": parts,
            "configuration": self.node_card(next(iter(configurations))),
            "source_context": boundaries,
            "truncated": walked["truncated"],
            "coverage": "documented_approvals" if parts else "no_approval_evidence",
            "note": "Compatibility only; installation authorisation is not established. "
            "If the question asks whether we have the part, query_stock gives its recorded "
            "stock.",
        }

    def search_passages(self, query: str, equipment_id: str | None, limit: int) -> dict:
        """Search eligible passages using words plus optional embedding similarity. Manuals
        must match the resolved equipment configuration; section numbers are irrelevant.
        Use the resolved parent machine for component guidance when the component has no
        configuration of its own. An empty result is a gap, not a reason to keep paraphrasing.
        Other records retain their source scope: a similar historical/line-level note is not
        a diagnosis of this machine. Null equipment searches site policy summaries only.
        """
        results = search_passages(query, self.passages(equipment_id), limit, self.embeddings)
        for result in results:
            self.remember(EvidenceCard.model_validate(result["card"]))
        return {
            "mode": "hybrid" if self.embeddings else "keyword",
            "results": results,
            "note": "Search ranking is not confidence or proof of a fault.",
        }

    def query_stock(self, equipment_id: str) -> dict:
        """Get reviewed stock counts for this machine's explicitly approved parts.
        Resolve equipment first. Returns approval paths plus protected stock summaries.
        Returns at most ten part/location summaries, selecting the latest dated count
        per exact part/location within the evidence cutoff;
        never adds snapshots or guesses location aliases. Equal-time conflicting counts
        stay unresolved. Missing counts/reservations remain unknown. These are historical
        records, not live availability; no free-stock calculation or permission to fit.
        """
        approvals = self.approved_parts(equipment_id)
        if approvals["coverage"] != "documented_approvals":
            return {"coverage": approvals["coverage"], "approvals": approvals, "stock": []}
        stock, truncated = [], approvals.get("truncated", False)
        for approved in approvals["parts"]:
            part_id = approved["node"]["node_id"]
            part_name = self.nodes[part_id].properties["part_id"]
            related = {
                e.source for e in self.edges if e.relation == "STOCK_OF" and e.target == part_id
            }
            groups = {}
            for identity in sorted(related):
                node = self.nodes[identity]
                groups.setdefault(node.properties["field_location"], []).append(node)
            for location, rows in sorted(groups.items()) or [(None, [])]:
                if len(stock) >= 10:
                    truncated = True
                    break
                newest = max(
                    (timestamp(r.properties["field_counted_at"]) for r in rows), default=None
                )
                selected = [
                    r for r in rows if timestamp(r.properties["field_counted_at"]) == newest
                ]
                columns = ("quantity", "quantity_on_hand", "quantity_reserved")
                variants = {
                    tuple((k, r.properties.get("field_" + k)) for k in columns) for r in selected
                }
                conflict = len(variants) > 1
                quantities = (
                    {k: v for k, v in next(iter(variants)) if v is not None}
                    if variants and not conflict
                    else {}
                )
                coverage = (
                    "conflicting_counts"
                    if conflict
                    else "recorded_counts"
                    if quantities
                    else "no_stock_count"
                )
                citations = [approved["path_citation"], approved["node"]["id"]]
                citations.extend(self.node_card(r.id)["id"] for r in selected)
                summary = {
                    "equipment_id": equipment_id,
                    "part_id": part_name,
                    "location": location,
                    "counted_at": newest.isoformat() if newest else None,
                    "quantities": quantities,
                    "coverage": coverage,
                    "records": [r.id for r in selected],
                }
                key = (
                    "stock-summary:"
                    + hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()[:20]
                )
                lines = [f"{part_name} · approved part for {equipment_id}"]
                if location is not None:
                    lines += [
                        f"Location: {location}",
                        f"Counted at: {newest.strftime('%d %b %Y, %H:%M (%z)')}",
                    ]
                if conflict:
                    lines.append(
                        "Conflicting counts at this time. No quantity selected; ask stores."
                    )
                elif quantities:
                    labels = {
                        "quantity": "Recorded quantity",
                        "quantity_on_hand": "Recorded on hand",
                        "quantity_reserved": "Recorded reserved",
                    }
                    lines.extend(f"{labels[k]}: {v}" for k, v in quantities.items())
                    if "quantity_reserved" not in quantities:
                        lines.append("Reservations: not recorded; do not assume zero.")
                    if not {"quantity", "quantity_on_hand"} & quantities.keys():
                        lines.append("Physical count: not recorded.")
                else:
                    lines.append("No reviewed stock count within the available records.")
                lines.append("Free stock is not calculated. Confirm availability with stores.")
                fact = ComputedFact(
                    id=key,
                    kind="stock_record",
                    text="\n".join(lines),
                    citations=citations,
                )
                self.facts[key] = fact
                stock.append({**summary, "record_summary_id": key})
        return {
            "coverage": "reviewed_snapshots_only",
            "truncated": truncated,
            "approvals": approvals,
            "stock": stock,
            "note": "Counts retain their source column meanings. Do not subtract or sum them "
            "or claim live availability. Different location labels have not been reconciled.",
        }

    def troubleshooting_context(self, equipment_id: str) -> dict:
        """For a stoppage or alarm on one resolved machine, gather its context in one graph
        traversal: installed configuration and the manual that applies to it (with its
        sections), recorded events on the machine and its components (with counts), the
        alarm definitions those events report, shift observations for it or its line, past
        maintenance cases and the site procedures for physical checks. Observations and
        suspicions stay labelled; past repairs are context, not today's diagnosis.
        """
        if equipment_id not in self.resolved:
            raise ValueError("Resolve the equipment before gathering its context.")
        self.node_card(equipment_id)
        applicability = self.walk_graph(
            equipment_id, ["HAS_CONFIGURATION", "APPLIES_TO"], "both", 2
        )["paths"]
        manuals = [p for p in applicability if self.nodes[p["node"]["node_id"]].kind == "Document"]
        sections = [
            self.remember(card)
            for card in self.passages(equipment_id)
            if self.documents[card.evidence[0].source_id].properties.get("role") == "manual"
        ]
        events = self.query_events(equipment_id, None, None)
        event_ids = {card["node_id"] for card in events.get("events", [])}
        alarms = {
            e.target for e in self.edges if e.relation == "REPORTS_ALARM" and e.source in event_ids
        }
        related = self.component_ids({equipment_id})
        lines = {
            e.source
            for e in self.edges
            if e.relation == "HAS_COMPONENT" and e.target in related | {equipment_id}
        }
        notes = sorted(
            (
                n
                for n in self.nodes.values()
                if n.kind == "Event"
                and n.properties["code"] in {"HANDOVER_NOTE", "UNVERIFIED_SUSPICION"}
                and n.properties["asset_id"] in related | lines
            ),
            key=lambda n: n.properties["occurred_at"],
        )
        history = self.query_history(equipment_id)
        procedures = {
            e.target
            for e in self.edges
            if e.relation == "REFERENCES" and e.source in {p["node"]["node_id"] for p in manuals}
        }
        policy = [self.remember(card) for card in self.passages(None)]
        return {
            "equipment": self.node_card(equipment_id),
            "applicable_manuals": [
                {"manual": p["node"], "applicability_path": p["path_citation"]} for p in manuals
            ],
            "manual_sections": sections,
            "recorded_events": {
                key: events[key]
                for key in ("count", "by_code", "coverage", "computed_fact", "events")
                if key in events
            },
            "alarm_definitions": [self.node_card(a) for a in sorted(alarms)],
            "shift_observations": [
                {
                    "kind": "unverified suspicion"
                    if n.properties["code"] == "UNVERIFIED_SUSPICION"
                    else "reported observation",
                    "card": self.node_card(n.id),
                }
                for n in notes
            ],
            "maintenance_history": [
                {
                    "record_summary_id": case["record_summary_id"],
                    "configuration_comparison": case["configuration_comparison"],
                }
                for case in history["cases"]
            ],
            "procedures": [self.node_card(p) for p in sorted(procedures)],
            "site_policy": policy,
            "note": "Records up to the question time, not live status. A manual's possible "
            "causes are possibilities to check, not a diagnosis.",
        }

    def definitions(self) -> list[dict]:
        """Expose typed function schemas and the tool docstrings to the query agent."""
        definitions = []
        finish_descriptions = {
            "finish_retrieval": "Finish when the evidence covers the question, when the "
            "technician must clarify (ask one neutral question), or when evidence is missing.",
        }
        for name, model in TOOL_MODELS.items():
            description = (
                finish_descriptions[name]
                if name in finish_descriptions
                else getattr(self, name).__doc__
            )
            parameters = model.model_json_schema()
            definitions.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                    "strict": True,
                }
            )
        return definitions

    def execute(self, name: str, arguments: dict) -> dict:
        """Validate a single call before dispatch; errors are data for the next agent decision."""
        if name not in TOOL_MODELS or name == "finish_retrieval":
            raise ValueError("Unknown retrieval tool.")
        parsed = TOOL_MODELS[name].model_validate(arguments)
        result = getattr(self, name)(**parsed.model_dump())
        observation = {"tool": name, "arguments": parsed.model_dump()}
        observation.update(
            {key: result[key] for key in ("coverage", "note", "truncated") if key in result}
        )
        if name == "query_history":
            observation["case_scope"] = [
                {
                    "case_id": case["case"]["node_id"],
                    "configuration_comparison": case["configuration_comparison"],
                }
                for case in result["cases"]
            ]
        self.observations.append(observation)
        return result


def windows(p: dict, size: int = 8) -> list[tuple[str, list[Evidence]]]:
    """Line windows of a document's text, for files without sections."""
    lines = p["text"].splitlines()
    pages = json.loads(p["page_numbers_json"]) if p.get("page_numbers_json") else None
    chunks = []
    for start in range(0, len(lines), size):
        refs = [
            Evidence(
                source_id=p["source_id"],
                path=p["path"],
                sha256=p["sha256"],
                line=i + 1,
                page=pages[i] if pages else None,
                excerpt=lines[i],
            )
            for i in range(start, min(start + size, len(lines)))
            if lines[i].strip()
        ]
        if refs:
            chunks.append((p.get("document_id", p["path"]), refs))
    return chunks


TOOL_MODELS = {
    "find_equipment": EquipmentQuery,
    "walk_graph": WalkQuery,
    "query_events": EventQuery,
    "search_passages": PassageQuery,
    "search_documents": DocumentQuery,
    "query_history": EquipmentScope,
    "approved_parts": EquipmentScope,
    "query_stock": EquipmentScope,
    "troubleshooting_context": EquipmentScope,
    "finish_retrieval": FinishQuery,
}
