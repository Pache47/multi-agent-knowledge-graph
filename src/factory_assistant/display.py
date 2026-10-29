"""Plain-language presentation, separate from graph identity and source evidence.

The small equipment-name catalogue is editorial wording for this fictional demo.
It does not create extraction facts or additional aliases for entity matching.
"""

import json

from factory_assistant.graph_models import Edge, GraphBundle, Node

EQUIPMENT_NAMES = {
    "CV-01": "Outfeed conveyor",
    "CV-02": "Case packer outfeed conveyor",
    "D-01": "Drive",
    "D-02": "Drive",
    "M-01": "Motor",
    "M-02": "Motor",
    "G-01": "Gearbox",
    "PE-01": "Entry sensor",
    "PE-02": "Exit sensor",
    "PLC-01": "Line controller",
    "LB-01": "Labeller",
    "LINE1": "Line 1",
    "LINE2": "Line 2",
}

RELATION_LABELS = {
    "HAS_COMPONENT": "has component",
    "SENDS_TO": "sends cartons to",
    "HAS_CONFIGURATION": "uses configuration",
    "RECORDED_FOR": "recorded for",
    "REPORTS_ALARM": "reports alarm",
    "STOCK_OF": "records stock of",
    "APPLIES_TO": "applies to",
    "REFERENCES": "refers to procedure",
    "DESCRIBES": "summarises procedure",
    "HAS_FINDING": "has finding",
    "APPROVED_FOR": "approved for",
    "DEFINES": "defines alarm",
}

KIND_LABELS = {
    "Equipment": "Equipment",
    "AlarmType": "Alarm type",
    "StockRecord": "Stock record",
    "MaintenanceCase": "Maintenance case",
    "Finding": "Finding",
}
EVENT_LABELS = {
    "MOTOR_OVERLOAD": "Motor overload",
    "CHANGEOVER_COMPLETE": "Changeover complete",
    "RUNNING": "Running",
    "READY": "Ready",
    "HANDOVER_NOTE": "Reported shift observation",
    "UNVERIFIED_SUSPICION": "Unverified suspicion",
}


def equipment_label(identity: str, multiline: bool = False) -> str:
    name = EQUIPMENT_NAMES.get(identity)
    if not name:
        return identity  # An unfamiliar ID must not acquire a guessed equipment role.
    return f"{name}\n({identity})" if multiline else f"{name} ({identity})"


def node_label(node: Node, multiline: bool = False) -> str:
    values = node.properties
    if node.kind == "MaintenanceCase":
        return f"Maintenance {values['case_id']} — {values['occurred_on']}"
    if node.kind == "Finding":
        return f"{values['status'].capitalize()} finding — {values['case_id']}"
    if node.kind == "Equipment":
        name = EQUIPMENT_NAMES.get(node.id) or node.properties.get("name")
        if name and name != node.id:
            return f"{name}\n({node.id})" if multiline else f"{name} ({node.id})"
        return node.id
    if node.kind == "Configuration":
        separator = "\n" if multiline else " — "
        return f"Model {values['model']}{separator}configuration {values['configuration']}"
    if node.kind == "AlarmType":
        return EVENT_LABELS.get(values.get("code"), node.id)
    if node.kind == "Event":
        code = values.get("code", "Event")
        return f"{EVENT_LABELS.get(code, code)} event ({values.get('event_id', node.id)})"
    if node.kind == "ProcedureReference":
        return (
            "Site maintenance procedure\nSummary only"
            if multiline
            else str(values.get("description", node.id))
        )
    if node.kind == "Document" and values.get("role") == "policy_summary":
        source = values["source_id"]
        return (
            f"Site maintenance policy\nSummary ({source})"
            if multiline
            else f"Site maintenance policy summary ({source})"
        )
    if node.kind == "Document" and values.get("role") == "manual":
        title = values.get("document_id") or str(values["path"]).rsplit("/", 1)[-1]
        details = [
            f"revision {values['revision']}" if "revision" in values else None,
            f"configuration {values['configuration']}" if "configuration" in values else None,
        ]
        details = " · ".join(d for d in details if d)
        separator = "\n" if multiline else " — "
        return f"Manual {title}{separator}{details}" if details else f"Manual {title}"
    if node.kind == "Part":
        return f"Part {values.get('part_id', node.id)}"
    if node.kind == "StockRecord":
        return (
            f"Stock count: {values['field_part_id']} · {values['field_location']} · "
            f"{values['field_counted_at']}"
        )
    return str(values.get("path", node.id))


def connection_label(edge: Edge, nodes: dict[str, Node]) -> str:
    return (
        f"{node_label(nodes[edge.source])} → {RELATION_LABELS[edge.relation]} → "
        f"{node_label(nodes[edge.target])}"
    )


def equipment_diagram(bundle: GraphBundle, relations: set[str] | None = None) -> str:
    """DOT keeps stable IDs as identifiers; labels alone change the visible wording."""
    shown_relations = (
        {"HAS_COMPONENT", "HAS_CONFIGURATION", "SENDS_TO"} if relations is None else relations
    )
    edges = [e for e in bundle.edges if e.relation in shown_relations]
    visible = {identity for edge in edges for identity in (edge.source, edge.target)}
    lines = ["digraph { rankdir=LR; node [shape=box, style=rounded];"]
    for node in bundle.nodes:
        if node.id in visible:
            label = json.dumps(node_label(node, True), ensure_ascii=False)
            lines.append(f"{json.dumps(node.id)} [label={label}];")
    for edge in edges:
        lines.append(
            f"{json.dumps(edge.source)} -> {json.dumps(edge.target)} "
            f"[label={json.dumps(RELATION_LABELS[edge.relation])}];"
        )
    lines.append("}")
    return "\n".join(lines)
