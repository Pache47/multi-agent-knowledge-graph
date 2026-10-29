"""One page per machine: setup, guidance, parts and stock, events and notes, history.

It uses the same read-only graph tools as the assistant, at the same records cutoff, so
what a technician browses here is exactly what the assistant can see.
"""

import json
from datetime import datetime

import streamlit as st

from factory_assistant.display import equipment_diagram, node_label
from factory_assistant.graph_models import Evidence
from factory_assistant.query_tools import GraphTools
from factory_assistant.settings_ui import readable_time


def show_sources(references):
    st.dataframe(
        [
            {"File": r.path.rsplit("/", 1)[-1], "Page": r.page, "Line": r.line, "Text": r.excerpt}
            for r in references
        ],
        hide_index=True,
        width="stretch",
        column_config={"Text": st.column_config.TextColumn(width="large")},
    )


def machines(bundle) -> list:
    """Equipment that has something to show: a setup, components, events or cases."""
    linked = {
        e.source for e in bundle.edges if e.relation in {"HAS_CONFIGURATION", "HAS_COMPONENT"}
    }
    linked |= {e.target for e in bundle.edges if e.relation == "RECORDED_FOR"}
    return sorted(
        (n for n in bundle.nodes if n.kind == "Equipment" and n.id in linked), key=lambda n: n.id
    )


def render_equipment(settings: dict):
    bundle = settings.get("bundle")
    if bundle is None:
        st.info("Build the knowledge first (Build & review).")
        return
    options = machines(bundle)
    labels = {n.id: node_label(n) for n in options}
    default = next((i for i, n in enumerate(options) if n.id.startswith(("CV", "BF"))), 0)
    selected = st.selectbox(
        "Equipment",
        [n.id for n in options],
        index=default,
        format_func=labels.get,
        help="Everything the records say about one machine, up to the time chosen in Settings.",
    )
    tools = GraphTools(bundle, settings["metadata"]["dataset"], settings["cutoff"], selected)
    tools.find_equipment(selected)
    if selected not in tools.resolved:
        st.info("No records for this equipment are available at the selected time.")
        return
    node = tools.nodes[selected]
    context = tools.troubleshooting_context(selected)
    stock = tools.query_stock(selected)
    aliases = ", ".join(node.properties.get("aliases", [])) or "none recorded"
    configurations = [
        tools.nodes[e.target]
        for e in tools.edges
        if e.source == selected and e.relation == "HAS_CONFIGURATION"
    ]
    parents = [
        e.source for e in tools.edges if e.relation == "HAS_COMPONENT" and e.target == selected
    ]
    children = [
        e.target for e in tools.edges if e.relation == "HAS_COMPONENT" and e.source == selected
    ]
    st.subheader(labels[selected])
    a, b, c = st.columns(3)
    a.metric(
        "Configuration",
        ", ".join(
            f"{n.properties['model']} · {n.properties['configuration']}" for n in configurations
        )
        or "not recorded",
        help="Model and configuration from the equipment list; they decide which manual and "
        "parts apply.",
    )
    b.metric(
        "Recorded events",
        context["recorded_events"].get("count") or 0,
        help="Alarms and stops in the controller logs, counted once even if two files repeat them.",
    )
    c.metric(
        "Maintenance cases",
        len(context["maintenance_history"]),
        help="Work orders and repair notes linked to this machine.",
    )
    st.caption(
        f"Also called: {aliases} · Part of: {', '.join(parents) or '—'} · "
        f"Components: {', '.join(children) or '—'} · Records up to {readable_time(tools.as_of)}"
    )

    guidance, parts, events, history = st.tabs(
        ["Guidance", "Parts & stock", "Events & shift notes", "Maintenance history"]
    )
    with guidance:
        manuals = context["applicable_manuals"]
        if not manuals:
            st.info("No manual is linked to this equipment's recorded configuration.")
        for manual in manuals:
            document = tools.nodes[manual["manual"]["node_id"]]
            st.markdown(f"**{node_label(document)}**")
            st.caption("Applies because the manual covers this equipment's recorded configuration.")
            if document.properties.get("archived_copy"):
                st.caption("Archived copy: check it against the site's controlled documents.")
            for section in json.loads(document.properties.get("sections_json", "[]")):
                with st.expander(section["heading"]):
                    st.text(section["text"])
                    show_sources([Evidence.model_validate(r) for r in section["evidence"]])
        for procedure in context["procedures"]:
            p = json.loads(procedure["text"])["properties"]
            note = (
                "" if p.get("details_available") else " The full procedure is not in these records."
            )
            st.warning(f"Physical work follows site procedure {p['procedure_id']}.{note}", icon="⚠️")
    with parts:
        approvals = stock.get("approvals", {})
        parts_found = [tools.nodes[p["node"]["node_id"]] for p in approvals.get("parts", [])]
        if not parts_found:
            st.info("No approved parts are recorded for this equipment's configuration.")
        for part in parts_found:
            description = part.properties.get("description", "")
            st.markdown(
                f"✅ **{part.properties['part_id']}** {description} — approved for this "
                "equipment's configuration"
            )
        for summary in stock["stock"]:
            fact = tools.facts[summary["record_summary_id"]]
            with st.expander("Recorded stock · " + fact.text.splitlines()[0], expanded=True):
                for line in fact.text.splitlines()[1:]:
                    st.write(line)
        context_cards = approvals.get("source_context", [])
        if context_cards:
            with st.expander("Approved parts list, as written"):
                seen = dict.fromkeys(card["text"] for card in context_cards)
                st.text("\n".join(seen))
    with events:
        rows = []
        for card in context["recorded_events"].get("events", []):
            p = json.loads(card["text"])["properties"]
            rows.append(
                {
                    "at": p["occurred_at"],
                    "Kind": "Controller event",
                    "Code": p["code"],
                    "Detail": p["message"],
                    "Recorded for": p["asset_id"],
                }
            )
        for item in context["shift_observations"]:
            p = json.loads(item["card"]["text"])["properties"]
            rows.append(
                {
                    "at": p["occurred_at"],
                    "Kind": item["kind"].capitalize(),
                    "Code": "",
                    "Detail": p["message"],
                    "Recorded for": p["asset_id"],
                }
            )
        if rows:
            rows = [
                {"Time": readable_time(r.pop("at")), **r}
                for r in sorted(rows, key=lambda r: datetime.fromisoformat(r["at"]))
            ]
            st.dataframe(rows, hide_index=True, width="stretch")
            st.caption("Duplicate exports count once. A shift note is a report, not a diagnosis.")
        else:
            st.info("No events or notes recorded for this equipment.")
    with history:
        if not context["maintenance_history"]:
            st.info("No maintenance cases recorded for this equipment.")
        for case in context["maintenance_history"]:
            fact = tools.facts[case["record_summary_id"]]
            with st.expander(fact.text.splitlines()[0]):
                for line in fact.text.splitlines()[1:]:
                    st.write(line)
                case_node = tools.nodes[case["record_summary_id"].removeprefix("history:")]
                show_sources(case_node.evidence)
    with st.expander("Plant map"):
        st.graphviz_chart(equipment_diagram(bundle), width="stretch")
