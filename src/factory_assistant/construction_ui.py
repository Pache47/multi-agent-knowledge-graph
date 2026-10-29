"""Data-owner workbench: add files, build the graph, review schema changes, see the evidence.

Navigation never starts processing; every model call needs an explicit button.
"""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import streamlit as st
from neo4j.exceptions import Neo4jError

from factory_assistant.display import RELATION_LABELS, node_label
from factory_assistant.graph_store import Neo4jProjection
from factory_assistant.models import FieldDefinition, ReviewDecision
from factory_assistant.pipeline import build_knowledge, gold_check, latest_bundle, save_bundle
from factory_assistant.review import SchemaReview
from factory_assistant.settings_ui import PLANT
from factory_assistant.uploads import SUFFIXES, add_file, added_files, remove_file

GOLD_FILES = ("construction_gold.json", "document_gold.json", "maintenance_gold.json")

PIPELINE_DOT = """digraph {
rankdir=LR; node [shape=box, style=rounded, fontsize=11];
files [label="Shared folder\\n(manifest)"];
triage [label="Triage agent\\nwhat is this file?"];
parser [label="Code parsers\\nlogs · registers"];
extract [label="Extraction agents\\none per document type"];
check [label="Code checks\\nquotes · dates · labels"];
resolve [label="Identity resolution\\ncode, then agent"];
review [label="Schema review\\n(human)"];
kg [label="Knowledge graph\\nwith source lines", shape=cylinder];
files -> triage -> parser -> resolve;
triage -> extract -> check -> resolve -> kg;
check -> extract [label="one correction", style=dashed];
resolve -> review [label="new fields", style=dashed];
review -> kg [style=dashed];
}"""


STEP_LABELS = {
    "register": "Registered the files",
    "equipment_registry": "Merged the equipment from every list and register",
    "resolve_identities": "Linked machine mentions to equipment (code first, then the agent)",
    "assemble_graph": "Assembled the graph; anything unverifiable is held",
}

NEO4J_QUERIES = """// The machine, its setup, and the manuals and parts that apply to that setup
MATCH (p:FactoryProject {name: 'bosch-assignment'})
MATCH (e:Equipment {id: 'CV-01', build: p.active_build})-[h:HAS_CONFIGURATION]->(c)
OPTIONAL MATCH (c)<-[a:APPLIES_TO|APPROVED_FOR]-(x)
RETURN e, h, c, a, x;

// Everything recorded for the machine and its components
MATCH (p:FactoryProject {name: 'bosch-assignment'})
MATCH (e:Equipment {id: 'CV-01', build: p.active_build})-[:HAS_COMPONENT*0..2]->(part)
MATCH (record)-[r:RECORDED_FOR]->(part)
RETURN part, r, record;

// The source lines behind one fact
MATCH (p:FactoryProject {name: 'bosch-assignment'})
MATCH (n:FactoryNode {id: 'part:RA-200B', build: p.active_build})
RETURN n.id, n.evidence_json;"""


def rebuild(root: Path, runtime: Path, mode: str = "replay", progress=None):
    bundle = build_knowledge(root, runtime, mode, progress=progress)
    save_bundle(runtime, bundle)
    return bundle


def file_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def show_step(status, step: str, update: dict):
    """One line per pipeline step; each file reports what triage made of it."""
    if step == "process_file":
        for result in update.get("files", []):
            held = len(result.held)
            status.write(
                f"📄 **{file_name(result.source.path)}** → {result.doc_type or 'not recognised'}"
                f" · {result.method} · {len(result.facts)} facts"
                + (f" · {held} held" if held else "")
            )
    elif step in STEP_LABELS:
        status.write(f"✓ {STEP_LABELS[step]}")


def build_with_progress(root: Path, runtime: Path, mode: str, label: str):
    with st.status(label, expanded=True) as status:
        bundle = rebuild(root, runtime, mode, lambda step, update: show_step(status, step, update))
        status.update(label="Knowledge saved", state="complete", expanded=False)
    return bundle


def cites(item, source_id: str) -> bool:
    return any(r.source_id == source_id for r in item.evidence)


def file_diagram(bundle, source_id: str) -> str | None:
    """The links this file supports, with the records they connect."""
    edges = [e for e in bundle.edges if cites(e, source_id) and e.relation != "STOCK_OF"]
    edges += [e for e in bundle.edges if e.relation == "STOCK_OF" and cites(e, source_id)][:6]
    if not edges:
        return None
    nodes = {n.id: n for n in bundle.nodes}
    shown = {i for e in edges[:40] for i in (e.source, e.target)}
    lines = ["digraph { rankdir=LR; node [shape=box, style=rounded, fontsize=10];"]
    for identity in shown:
        new = cites(nodes[identity], source_id)
        style = ', style="rounded,filled", fillcolor="#dbeafe"' if new else ""
        label = json.dumps(node_label(nodes[identity], True), ensure_ascii=False)
        lines.append(f"{json.dumps(identity)} [label={label}{style}];")
    for edge in edges[:40]:
        lines.append(
            f"{json.dumps(edge.source)} -> {json.dumps(edge.target)} "
            f"[label={json.dumps(RELATION_LABELS[edge.relation])}, fontsize=9];"
        )
    lines.append("}")
    return "\n".join(lines)


def render_added_result(bundle, source_id: str, before: dict | None):
    """What the agents made of one added file, and what changed in the graph."""
    trace = next((t for t in bundle.extraction_trace if t["source_id"] == source_id), None)
    if trace is None:
        return
    st.markdown(f"#### What happened to {file_name(trace['path'])}")
    triage = next((s for s in trace["steps"] if s["stage"] == "triage"), None)
    held = [h for h in bundle.held if h["source"] == source_id]
    records = [n for n in bundle.nodes if cites(n, source_id) and n.kind != "Document"]
    links = [e for e in bundle.edges if cites(e, source_id)]
    a, b, c, d = st.columns(4)
    a.metric("Recognised as", (trace["doc_type"] or "—").replace("_", " "))
    b.metric("Records citing it", len(records))
    c.metric("Links citing it", len(links))
    d.metric("Held for review", len(held))
    if triage:
        decision = triage["decision"]
        st.caption(f"Triage agent: {decision['reason']} · Route: {trace['method']}")
    if before is not None:
        nodes = {n.id for n in bundle.nodes} - set(before["nodes"])
        edges = {(e.source, e.relation, e.target) for e in bundle.edges}
        new_edges = edges - {tuple(e) for e in before["edges"]}
        st.caption(
            f"Compared with the previous build: {len(nodes)} new records and "
            f"{len(new_edges)} new links in the graph."
        )
    diagram = file_diagram(bundle, source_id)
    if diagram:
        st.graphviz_chart(diagram, width="stretch")
        st.caption("Shaded records cite this file; the others were already in the graph.")
    elif not held:
        st.info("Nothing in this file was linked into the graph.")
    if records:
        with st.expander("Records and the lines they cite"):
            st.dataframe(
                [
                    {
                        "Record": node_label(n),
                        "Kind": n.kind,
                        "Lines": ", ".join(
                            str(r.line) for r in n.evidence if r.source_id == source_id
                        ),
                    }
                    for n in records
                ],
                hide_index=True,
                width="stretch",
            )
    if held:
        with st.expander("Held: kept out of answers until a person resolves them", expanded=True):
            st.dataframe(held, hide_index=True, width="stretch")
    with st.expander("Agent decisions and checks"):
        st.json(trace["steps"])


def render_add_file(root: Path, runtime: Path, bundle):
    st.subheader("➕ Add a file")
    st.write(
        "Drop in a file from the shared folder: a controller log, shift note, work order, "
        "stores export, equipment list or PDF manual. The triage agent decides what it is; "
        "only this file goes to the agents, the rest is reused."
    )
    with st.form("add-file", clear_on_submit=True):
        upload = st.file_uploader(
            "File",
            type=sorted(s.lstrip(".") for s in SUFFIXES),
            max_upload_size=2,
            help="The whole folder is rebuilt with this file; its records then appear in answers "
            "and on the Equipment tab. You can remove it again below.",
        )
        st.caption(
            "The file is dated now, so views of earlier times (such as the incident) "
            "will not see it. Live agents read it, so it is sent to OpenAI."
        )
        submitted = st.form_submit_button("Add and build", type="primary")
    if submitted:
        if upload is None:
            st.warning("Choose a file first.")
            return bundle
        recorded = datetime.now(PLANT).replace(microsecond=0).isoformat()
        try:
            entry = add_file(runtime, upload.name, upload.getvalue(), recorded)
            before = (
                {
                    "nodes": [n.id for n in bundle.nodes],
                    "edges": [(e.source, e.relation, e.target) for e in bundle.edges],
                }
                if bundle
                else None
            )
            bundle = build_with_progress(root, runtime, "live", f"Building with {upload.name}…")
            st.session_state["added"] = {"id": entry["id"], "before": before}
        except ValueError as exc:
            st.error(f"The file was not added: {exc}")
    return bundle


def render_added_files(root: Path, runtime: Path):
    entries = added_files(runtime)
    if not entries:
        return
    with st.expander(f"Files added here ({len(entries)})"):
        st.caption("Stored with this app's runtime data, never in the customer's folder.")
        for entry in entries:
            left, right = st.columns([5, 1])
            left.write(f"**{entry['file']}** · {entry['id']} · added {entry['recorded_at']}")
            if right.button("Remove", key=f"remove-{entry['id']}"):
                remove_file(runtime, entry["id"])
                st.session_state.pop("added", None)
                rebuild(root, runtime, "live")
                st.rerun()


def render_build(root: Path, runtime: Path):
    bundle = latest_bundle(runtime)
    if bundle is not None:
        a, b, c, d = st.columns(4)
        a.metric("Files", len(bundle.extraction_trace), help="Files read in the last build.")
        b.metric(
            "Graph records",
            len(bundle.nodes),
            help="Machines, parts, events, work orders and other facts, each citing its lines.",
        )
        c.metric(
            "Links",
            len(bundle.edges),
            help="Connections between records, such as a part approved for a configuration.",
        )
        d.metric(
            "Held items",
            len(bundle.held),
            help="Facts kept out of answers because they are ambiguous or conflict, until a "
            "person resolves them.",
        )
    with st.expander("Build the whole folder", expanded=bundle is None):
        st.graphviz_chart(PIPELINE_DOT, width="stretch")
        st.caption(
            "Every file is triaged by content. Structured exports use code parsers; text and "
            "PDFs use an extraction agent whose quotes code checks against the file. Nothing "
            "depends on file names."
        )
        mode = st.radio(
            "Agent decisions",
            ["replay", "live"],
            horizontal=True,
            help="Replay reuses the agents' recorded decisions, so the build is repeatable and "
            "free. Live asks the agents again for files that are new or changed.",
            format_func=lambda value: (
                "Replay recorded decisions (no model calls)"
                if value == "replay"
                else "Live — call OpenAI for new or changed files"
            ),
        )
        if st.button("Build graph", type="primary"):
            try:
                bundle = build_with_progress(root, runtime, mode, "Building the knowledge…")
                st.session_state.pop("added", None)
                st.success("Knowledge saved. Ask about a problem, or inspect it below.")
            except ValueError as exc:
                st.error(f"Build failed: {exc}")
    if bundle is None:
        st.info("Build the knowledge first.")
        return
    bundle = render_add_file(root, runtime, bundle)
    added = st.session_state.get("added")
    if added:
        render_added_result(bundle, added["id"], added["before"])
    render_added_files(root, runtime)

    st.subheader("📂 Files in the knowledge")
    st.dataframe(
        [
            {
                "File": file_name(t["path"]),
                "Recognised as": (t["doc_type"] or "—").replace("_", " "),
                "Route": t["method"],
                "Agent calls": sum(1 for s in t["steps"] if "agent" in s),
                "Corrected": any(s["stage"] == "correct" for s in t["steps"]),
                "Facts": t["facts"],
                "Held": sum(1 for h in bundle.held if h["source"] == t["source_id"]),
            }
            for t in bundle.extraction_trace
        ],
        hide_index=True,
        width="stretch",
    )
    with st.expander("Look at one file: what it added and the agents' decisions"):
        names = [t["source_id"] for t in bundle.extraction_trace]
        chosen = st.selectbox(
            "File",
            names,
            format_func=lambda key: next(
                file_name(t["path"]) for t in bundle.extraction_trace if t["source_id"] == key
            ),
        )
        render_added_result(bundle, chosen, None)
    if bundle.held:
        with st.expander(f"Held items ({len(bundle.held)}): kept out of answers until resolved"):
            st.dataframe(bundle.held, hide_index=True, width="stretch")
    with st.expander("Developer tools: gold checks, export, Neo4j"):
        reports = [gold_check(bundle, root / "evaluation" / name) for name in GOLD_FILES]
        passed, total = sum(r["passed"] for r in reports), sum(r["total"] for r in reports)
        st.write(
            f"**Gold checks: {passed} / {total}** · build {bundle.fingerprint[:10]} · "
            f"{bundle.mode}. Hand-written development assertions, never shown to the agents."
        )
        st.dataframe([c for r in reports for c in r["checks"]], hide_index=True)
        st.download_button(
            "Download graph evidence",
            bundle.model_dump_json(indent=2),
            file_name="graph-evidence.json",
            mime="application/json",
        )
        st.write("Chat reads the saved graph in memory; a Neo4j copy supports inspection.")
        if st.button("Publish saved graph to Neo4j"):
            try:
                with Neo4jProjection(root) as graph:
                    result = graph.publish(bundle)
                st.success(
                    f"Published {result['nodes']} records and {result['relationships']} links. "
                    "Open http://localhost:7474 and try the queries below."
                )
            except (ValueError, Neo4jError):
                st.warning("Neo4j is unavailable. The saved knowledge is intact.")
        st.code(NEO4J_QUERIES, language="cypher")


TYPE_LABELS = {
    "string": "Text",
    "integer": "Whole number",
    "number": "Number",
    "boolean": "True or false",
}


def render_review(root: Path, runtime: Path):
    """A proposal card: the new columns, their proposed meaning, the source line, a decision."""
    st.subheader("Review schema changes")
    st.write(
        "When a file has columns the knowledge model does not define, its records wait here. "
        "Other files are not blocked. Approval changes the schema, not the truth of any count."
    )
    review = SchemaReview(runtime / "review.sqlite")
    schema = review.schema()
    with st.expander(f"Stock fields currently approved (schema version {schema.version})"):
        st.dataframe(
            [
                {"Field": f.name, "Type": f.data_type, "Meaning": f.description}
                for f in schema.fields
            ],
            hide_index=True,
        )
    proposals = review.proposals()
    if not proposals:
        st.info("No proposals yet. Build the knowledge to process the stock exports.")
        return
    for proposal in proposals:
        open_ = proposal["status"] in {"pending", "deferred"}
        title = f"{Path(proposal['source_path']).name} · {proposal['status']}"
        with st.expander(title, expanded=open_):
            st.code(proposal["evidence"], language="text")
            rows = [
                {
                    "Column": f["name"],
                    "Type": TYPE_LABELS[f["data_type"]],
                    "Meaning": f["description"],
                }
                for f in proposal["fields"]
            ]
            if not open_:
                st.dataframe(rows, hide_index=True)
                st.caption("Decision history")
                st.dataframe(review.history(proposal["id"]), hide_index=True)
                continue
            with st.form(f"review-{proposal['fingerprint']}"):
                edited = st.data_editor(
                    rows,
                    hide_index=True,
                    num_rows="fixed",
                    disabled=["Column"],
                    column_config={
                        "Type": st.column_config.SelectboxColumn(
                            options=list(TYPE_LABELS.values()), required=True
                        ),
                        "Meaning": st.column_config.TextColumn(required=True, width="large"),
                    },
                    width="stretch",
                )
                reviewer = st.text_input("Reviewer name")
                note = st.text_input("Note (optional)")
                action = st.selectbox("Decision", ["approve", "edit", "reject", "defer"])
                st.caption("Edit saves a revised proposal for another review; it does not approve.")
                submitted = st.form_submit_button("Save decision")
            if submitted:
                try:
                    reverse = {label: value for value, label in TYPE_LABELS.items()}
                    revised = [
                        FieldDefinition(
                            name=row["Column"],
                            data_type=reverse[row["Type"]],
                            description=row["Meaning"],
                        )
                        for row in edited
                    ]
                    if (
                        action == "approve"
                        and [f.model_dump() for f in revised] != proposal["fields"]
                    ):
                        raise ValueError("Save your edits with Edit, then approve the revision.")
                    review.decide(
                        proposal["id"],
                        ReviewDecision(
                            action=action,
                            reviewer=reviewer.strip(),
                            note=note,
                            proposal_fingerprint=proposal["fingerprint"],
                            edited_fields=revised if action == "edit" else None,
                            event_id=str(uuid4()),
                        ),
                    )
                    if action == "approve" and latest_bundle(runtime) is not None:
                        # Replay is instant and model-free: approved records publish now.
                        rebuild(root, runtime)
                    st.rerun()
                except (ValueError, TypeError) as exc:
                    st.error(f"Decision was not applied: {exc}")


def pending_reviews(runtime: Path) -> int:
    return sum(
        p["status"] in {"pending", "deferred"}
        for p in SchemaReview(runtime / "review.sqlite").proposals()
    )


def render_construction(root: Path, runtime: Path):
    pending = pending_reviews(runtime)
    review_label = f"Review schema changes ({pending})" if pending else "Review schema changes"
    view = st.radio(
        "Workbench",
        ["Build knowledge", review_label],
        horizontal=True,
        key="workbench",
        help="Build turns the files into the knowledge graph. Review is where a person approves "
        "new stock fields the agents propose.",
    )
    if view == "Build knowledge":
        render_build(root, runtime)
    else:
        render_review(root, runtime)
