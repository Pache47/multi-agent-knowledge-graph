"""One construction pipeline for any registered folder, coordinated with LangGraph.

    register → process_file (one worker per file, in parallel) → equipment registry
             → resolve identities → assemble graph (incl. schema review gate) → saved KG

Each worker asks the triage agent what the file is, then routes it: structured exports
(controller logs, equipment registers) go to deterministic parsers; free text and PDFs
go to that type's extraction agent, whose quotes code checks, with one correction if
checks fail. Nothing in the pipeline depends on a file name or source ID.
"""

from __future__ import annotations

import csv
import json
import operator
from pathlib import Path
from typing import Annotated, TypedDict
from zoneinfo import ZoneInfo

from filelock import FileLock
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from openai import OpenAIError
from pydantic import ValidationError

from factory_assistant.agents import ConstructionAgents, MissingDecision, ModelAgents
from factory_assistant.assemble import (
    FileResult,
    GraphBuilder,
    Resolver,
    assemble,
    build_equipment,
    bundle_from,
    resolve_file,
)
from factory_assistant.evidence import MAIN_FOLDER, load_manifest, read_registered
from factory_assistant.extract import convert, coverage_hints, substantive
from factory_assistant.graph_models import GraphBundle
from factory_assistant.graph_names import upgrade_saved_graph
from factory_assistant.models import SourceSnapshot
from factory_assistant.parsers import parse_equipment_csv, parse_event_log
from factory_assistant.review import SchemaReview
from factory_assistant.uploads import FOLDER, added_files, upload_dir

AGENT_FAILURES = (ValueError, OpenAIError, ValidationError)


class BuildState(TypedDict, total=False):
    sources: list[SourceSnapshot]
    files: Annotated[list[FileResult], operator.add]
    held: Annotated[list[dict], operator.add]
    builder: GraphBuilder
    resolutions: dict


class KnowledgeBuilder:
    def __init__(
        self,
        root: Path,
        folder: str,
        review: SchemaReview,
        agents: ConstructionAgents,
        mode: str,
        runtime: Path | None = None,
    ):
        self.root, self.review, self.agents, self.mode = root, review, agents, mode
        self.manifest = load_manifest(root, folder)
        # Files added in the app are read after the registered folder, like any other file.
        self.added = upload_dir(runtime) if runtime else None
        if runtime:
            known = {s["id"] for s in self.manifest["sources"]}
            self.manifest["sources"] += [
                {**entry, "path": f"{FOLDER}/{entry['path']}", "added": True}
                for entry in added_files(runtime)
                if entry["id"] not in known
            ]
        self.zone = ZoneInfo(self.manifest["timezone"])
        self.graph = self._graph().compile()

    def _graph(self) -> StateGraph:
        graph = StateGraph(BuildState)
        graph.add_node("register", self.register)
        graph.add_node("process_file", self.process_file)
        graph.add_node("equipment_registry", self.equipment_registry)
        graph.add_node("resolve_identities", self.resolve_identities)
        graph.add_node("assemble_graph", self.assemble_graph)
        graph.add_edge(START, "register")
        graph.add_conditional_edges(
            "register",
            lambda state: (
                [Send("process_file", {"source": s}) for s in state["sources"]]
                or "equipment_registry"
            ),
            ["process_file", "equipment_registry"],
        )
        graph.add_edge("process_file", "equipment_registry")
        graph.add_edge("equipment_registry", "resolve_identities")
        graph.add_edge("resolve_identities", "assemble_graph")
        graph.add_edge("assemble_graph", END)
        return graph

    def run(self, progress=None) -> GraphBundle:
        """progress(step, update) is called as each pipeline step finishes."""
        builder = None
        for chunk in self.graph.stream({"files": [], "held": []}, stream_mode="updates"):
            for step, update in chunk.items():
                if update and "builder" in update:
                    builder = update["builder"]
                if progress:
                    progress(step, update or {})
        return bundle_from(builder, self.mode)

    # Nodes --------------------------------------------------------------------------

    def register(self, state: BuildState) -> dict:
        sources, held = read_registered(self.root, self.manifest, self.added)
        return {"sources": sources, "held": held}

    def process_file(self, state: dict) -> dict:
        return {"files": [self.process(state["source"])]}

    def equipment_registry(self, state: BuildState) -> dict:
        order = {s["id"]: i for i, s in enumerate(self.manifest["sources"])}
        files = sorted(state["files"], key=lambda r: order[r.source.source_id])
        builder = GraphBuilder(self.manifest["dataset"])
        builder.held.extend(state["held"])
        for result in files:
            builder.held.extend({"source": result.source.source_id, **h} for h in result.held)
        build_equipment(builder, files)
        return {"builder": builder}

    def resolve_identities(self, state: BuildState) -> dict:
        builder = state["builder"]
        resolver = Resolver([n for n in builder.nodes.values() if n.kind == "Equipment"])
        resolutions = {}
        for result in self.ordered(state):
            if result.doc_type in {"event_log", "handover", "maintenance_history"}:
                resolutions[result.source.source_id] = resolve_file(
                    result, resolver, self.agents, AGENT_FAILURES
                )
        return {"resolutions": resolutions}

    def assemble_graph(self, state: BuildState) -> dict:
        builder = state["builder"]
        files = self.ordered(state)
        assemble(builder, files, state["resolutions"], self.review, self.zone)
        builder.trace.extend(
            {
                "source_id": r.source.source_id,
                "path": r.source.path,
                "doc_type": r.doc_type,
                "method": r.method,
                "facts": len(r.facts),
                "steps": r.trace,
            }
            for r in files
        )
        return {}

    def ordered(self, state: BuildState) -> list[FileResult]:
        order = {s["id"]: i for i, s in enumerate(self.manifest["sources"])}
        return sorted(state["files"], key=lambda r: order[r.source.source_id])

    # One file -----------------------------------------------------------------------

    def process(self, source: SourceSnapshot) -> FileResult:
        result = FileResult(source=source, doc_type=None, method="held")
        try:
            triage, meta = self.agents.triage(source)
        except AGENT_FAILURES as exc:
            result.held.append({"stage": "triage", "reason": str(exc)})
            return result
        result.trace.append({**meta, "stage": "triage", "decision": triage.model_dump()})
        result.doc_type = triage.doc_type
        if triage.doc_type == "unsupported":
            result.held.append({"stage": "triage", "reason": "Unsupported: " + triage.reason})
            return result
        if triage.doc_type == "event_log":
            result.method = "parser: controller log"
            result.facts, rows = parse_event_log(source, self.zone)
            result.held.extend({"stage": "parser", **row} for row in rows)
            if not result.facts:
                result.held.append({"stage": "parser", "reason": "No rows fit the log layout."})
            return result
        if triage.doc_type == "equipment_list" and source.path.lower().endswith(".csv"):
            try:
                result.facts = parse_equipment_csv(source)
                result.method = "parser: equipment register"
                return result
            except (ValueError, csv.Error) as exc:
                # A different layout is not an error: let the extraction agent read it.
                result.trace.append({"stage": "parser", "fallback": str(exc)})
        result.method = "agent: extraction"
        try:
            result.facts, problems = self.extract(source, triage.doc_type, result.trace)
            # Try another approach when the first reading produced nothing usable.
            alternative = triage.alternative
            if (
                not substantive(result.facts)
                and alternative
                and alternative not in {"unsupported", "event_log", triage.doc_type}
            ):
                facts, retry = self.extract(source, alternative, result.trace)
                result.trace.append(
                    {
                        "stage": "reroute",
                        "from": triage.doc_type,
                        "to": alternative,
                        "kept": substantive(facts),
                    }
                )
                if substantive(facts):
                    result.doc_type, result.facts, problems = alternative, facts, retry
            result.held.extend({"stage": "extraction", "reason": p} for p in problems)
        except AGENT_FAILURES as exc:
            stage = "replay" if isinstance(exc, MissingDecision) else "extraction"
            result.held.append({"stage": stage, "reason": str(exc)})
        return result

    def extract(self, source: SourceSnapshot, doc_type: str, trace: list) -> tuple[list, list]:
        """Extract, check against the source, and allow one correction from the feedback."""
        decision, meta = self.agents.extract(doc_type, source)
        trace.append(
            {**meta, "stage": "extract", "doc_type": doc_type, "decision": decision.model_dump()}
        )
        facts, problems = convert(doc_type, source, decision, self.zone)
        hints = coverage_hints(doc_type, source, decision)
        if not problems and not hints:
            return facts, problems
        try:
            corrected, meta = self.agents.extract(
                doc_type, source, previous=decision, feedback=problems + hints
            )
        except AGENT_FAILURES as exc:
            # Facts that already passed their checks stand; only the problems stay held.
            trace.append({"stage": "correct", "failed": str(exc)})
            return facts, problems + [f"Correction not available: {exc}"]
        retry, remaining = convert(doc_type, source, corrected, self.zone)
        still = coverage_hints(doc_type, source, corrected)
        trace.append(
            {
                **meta,
                "stage": "correct",
                "feedback": problems + hints,
                "decision": corrected.model_dump(),
                "remaining": remaining + still,
            }
        )
        # Keep the correction only if it fixed something without losing facts.
        if len(remaining) + len(still) < len(problems) + len(hints) and len(retry) >= len(facts):
            return retry, remaining
        return facts, problems


def build_knowledge(
    root: Path,
    runtime: Path,
    mode: str = "replay",
    folder: str = MAIN_FOLDER,
    agents: ConstructionAgents | None = None,
    refresh: bool = False,
    progress=None,
) -> GraphBundle:
    """Build the knowledge graph for one folder and any files added in the app.

    Replay mode never calls a model. Decisions about added files are kept with them in
    the runtime folder, so the committed agent_cache only records the registered folder.
    """
    if mode not in {"replay", "live"}:
        raise ValueError("Choose replay or live.")
    agents = agents or ModelAgents(
        root / "agent_cache", mode, refresh=refresh, added_cache=upload_dir(runtime) / "decisions"
    )
    review = SchemaReview(runtime / "review.sqlite")
    return KnowledgeBuilder(root, folder, review, agents, mode, runtime).run(progress)


# Saved builds ---------------------------------------------------------------------


def save_bundle(runtime: Path, bundle: GraphBundle) -> Path:
    """Keep old snapshots and switch the pointer atomically."""
    folder = runtime / "graphs"
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(folder / ".save.lock")):
        target = folder / f"{bundle.fingerprint}.json"
        target.write_text(bundle.model_dump_json(indent=2))
        pointer = folder / "latest.tmp"
        pointer.write_text(target.name)
        pointer.replace(folder / "latest")
        return target


def latest_bundle(runtime: Path) -> GraphBundle | None:
    folder = runtime / "graphs"
    pointer = folder / "latest"
    if not pointer.exists():
        return None
    with FileLock(str(folder / ".save.lock")):
        path = folder / pointer.read_text().strip()
        if path.parent != folder:
            raise ValueError("Invalid saved graph path.")
        return GraphBundle.model_validate(upgrade_saved_graph(json.loads(path.read_text())))


def gold_check(bundle: GraphBundle, gold_path: Path) -> dict:
    """Hand-written expected facts, kept outside every input the agents see."""
    gold = json.loads(gold_path.read_text())
    actual_edges = {(e.source, e.relation, e.target) for e in bundle.edges}
    nodes = {n.id: n for n in bundle.nodes}
    checks = []
    for expected in gold["relationships"]:
        checks.append({"check": " → ".join(expected), "passed": tuple(expected) in actual_edges})
    for forbidden in gold["forbidden_relationships"]:
        checks.append(
            {
                "check": "Absent: " + " → ".join(forbidden),
                "passed": tuple(forbidden) not in actual_edges,
            }
        )
    for identity, properties in gold["properties"].items():
        node = nodes.get(identity)
        checks.append(
            {
                "check": f"Properties: {identity}",
                "passed": node is not None
                and all(node.properties.get(k) == v for k, v in properties.items()),
            }
        )
    for identity, count in gold.get("evidence_counts", {}).items():
        checks.append(
            {
                "check": f"Source references: {identity}",
                "passed": identity in nodes and len(nodes[identity].evidence) == count,
            }
        )
    if "overloads" in gold:
        overloads = sum(
            n.kind == "Event" and n.properties.get("code") == "MOTOR_OVERLOAD" for n in bundle.nodes
        )
        checks.append(
            {"check": "Distinct overload events", "passed": overloads == gold["overloads"]}
        )
    return {"passed": sum(c["passed"] for c in checks), "total": len(checks), "checks": checks}
