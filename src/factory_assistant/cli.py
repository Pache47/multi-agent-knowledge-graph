"""Command line: build the knowledge graph, review schema changes, ask questions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from factory_assistant.models import FieldDefinition, ReviewDecision

GOLD_FILES = ("construction_gold.json", "document_gold.json", "maintenance_gold.json")


def main():
    parser = argparse.ArgumentParser(description="Factory knowledge graph and assistant")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--runtime", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build the knowledge graph from the shared folder")
    build.add_argument(
        "--mode",
        choices=["replay", "live"],
        default="replay",
        help="replay recorded agent decisions (no model calls) or run live agents",
    )
    build.add_argument("--refresh", action="store_true", help="live: ignore recorded decisions")
    build.add_argument("--folder", default="data/shared_folder")
    build.add_argument("--publish", action="store_true", help="Also project the build to Neo4j")
    commands.add_parser("graph-sync", help="Retry projection of the latest saved build")
    commands.add_parser("proposals", help="List schema-change proposals")
    review = commands.add_parser("review", help="Decide a schema-change proposal")
    review.add_argument("proposal_id")
    review.add_argument("action", choices=["approve", "edit", "reject", "defer"])
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note", default="")
    review.add_argument("--fields", type=Path, help="Edited field-definition JSON list")
    review.add_argument("--fingerprint", required=True, help="Fingerprint shown by 'proposals'")
    answer = commands.add_parser("ask", help="Investigate a question using bounded graph tools")
    answer.add_argument("question")
    answer.add_argument("--mode", choices=["offline", "live"], default="offline")
    answer.add_argument(
        "--as-of",
        default=None,
        help="Timezone-aware cutoff, or latest for all reviewed records; defaults to scenario",
    )
    answer.add_argument("--scenario", default="main", help="main or a folder in data/scenarios")
    answer.add_argument("--search", choices=["keyword", "hybrid"], default="hybrid")
    args = parser.parse_args()
    root = args.root.resolve()
    runtime = args.runtime or root / ".runtime"
    load_dotenv(root / ".env")
    try:
        if args.command == "ask":
            from factory_assistant.assistant import answer_question
            from factory_assistant.scenarios import select_scenario

            metadata, bundle = select_scenario(root, runtime, args.scenario)
            result = answer_question(
                bundle,
                args.question,
                metadata["latest_question_at"]
                if args.as_of == "latest"
                else args.as_of or metadata["question_at"],
                metadata["dataset"],
                runtime,
                mode=args.mode,
                search_mode=args.search,
            )
            print(result.model_dump_json(indent=2))
            return
        if args.command in {"build", "graph-sync"}:
            from factory_assistant.graph_store import Neo4jProjection
            from factory_assistant.pipeline import (
                build_knowledge,
                gold_check,
                latest_bundle,
                save_bundle,
            )

            if args.command == "build":
                bundle = build_knowledge(
                    root, runtime, args.mode, args.folder, refresh=args.refresh
                )
                path = save_bundle(runtime, bundle) if args.folder == "data/shared_folder" else None
                result = {
                    "saved": str(path) if path else None,
                    "nodes": len(bundle.nodes),
                    "relationships": len(bundle.edges),
                    "files": [
                        {k: t[k] for k in ("source_id", "doc_type", "method", "facts")}
                        for t in bundle.extraction_trace
                    ],
                    "held": bundle.held,
                }
                if args.folder == "data/shared_folder":
                    reports = [gold_check(bundle, root / "evaluation" / g) for g in GOLD_FILES]
                    result["gold_checks"] = {
                        "passed": sum(r["passed"] for r in reports),
                        "total": sum(r["total"] for r in reports),
                        "failed": [
                            c["check"] for r in reports for c in r["checks"] if not c["passed"]
                        ],
                    }
            else:
                bundle = latest_bundle(runtime)
                if bundle is None:
                    raise ValueError("Build a graph before syncing it.")
                result = {}
            if args.command == "graph-sync" or args.publish:
                with Neo4jProjection(root) as graph:
                    result["neo4j"] = graph.publish(bundle)
            print(json.dumps(result, indent=2))
            return
        from factory_assistant.review import SchemaReview

        review_store = SchemaReview(runtime / "review.sqlite")
        if args.command == "proposals":
            result = review_store.proposals()
        else:
            fields = None
            if args.fields:
                fields = [
                    FieldDefinition.model_validate(f) for f in json.loads(args.fields.read_text())
                ]
            result = review_store.decide(
                args.proposal_id,
                ReviewDecision(
                    action=args.action,
                    reviewer=args.reviewer,
                    note=args.note,
                    proposal_fingerprint=args.fingerprint,
                    edited_fields=fields,
                    event_id=str(uuid4()),
                ),
            )
        print(json.dumps(result, indent=2))
    except ValueError as exc:
        parser.exit(2, f"Could not complete request: {exc}\n")


if __name__ == "__main__":
    main()
