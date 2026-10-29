"""Display helpers for the construction walkthrough notebooks (concise and detailed).

Only presentation lives here (tables, figures, short text views). Every pipeline step
is called directly in the notebook from `factory_assistant`.
"""

from __future__ import annotations

import textwrap

import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from IPython.display import Markdown, display
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch

# Ten node kinds folded into seven groups, coloured in a fixed categorical order.
GROUPS = {
    "Equipment": ("Equipment", "#2a78d6"),
    "Configuration": ("Configuration", "#eb6834"),
    "Document": ("Document", "#1baf7a"),
    "Event": ("Event / alarm", "#eda100"),
    "AlarmType": ("Event / alarm", "#eda100"),
    "MaintenanceCase": ("Maintenance case / finding", "#e87ba4"),
    "Finding": ("Maintenance case / finding", "#e87ba4"),
    "Part": ("Part / stock", "#008300"),
    "StockRecord": ("Part / stock", "#008300"),
    "ProcedureReference": ("Procedure", "#6250d6"),
}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d6d5d0"

pd.set_option("display.max_colwidth", 110)
pd.set_option("display.max_rows", 80)


def note(text: str):
    """A short highlighted takeaway under a result."""
    display(Markdown("> " + text))


def numbered_view(source, start: int = 1, end: int | None = None, pages: bool = False):
    """The file as an agent sees it: every line keeps the number citations will use."""
    lines = source.text.splitlines()
    end = min(end or len(lines), len(lines))
    for n in range(start, end + 1):
        page = f"p{source.page_numbers[n - 1]} " if pages and source.page_numbers else ""
        print(f"{page}{n:>3}: {lines[n - 1]}")


def evidence_frame(evidence) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "source": e.source_id,
                "file": e.path.rsplit("/", 1)[-1],
                "line": e.line,
                "page": e.page or "",
                "exact text in the file": e.excerpt,
            }
            for e in evidence
        ]
    )


def short(text, width: int = 90) -> str:
    return textwrap.shorten(str(text), width=width, placeholder=" …")


# Figures ------------------------------------------------------------------------------

PIPELINE_ORDER = [
    "__start__",
    "register",
    "process_file",
    "equipment_registry",
    "resolve_identities",
    "assemble_graph",
    "__end__",
]
PIPELINE_CAPTIONS = {
    "__start__": "folder\nmanifest",
    "register": "register\nread every file",
    "process_file": "process_file\none worker per file\ntriage → parse, or\nextract → check",
    "equipment_registry": "equipment_registry\nmerge equipment\nfrom all files",
    "resolve_identities": "resolve_identities\ncode first,\nthen agent",
    "assemble_graph": "assemble_graph\nnodes + links\n(schema review gate)",
    "__end__": "knowledge\ngraph",
}


def draw_pipeline(compiled):
    """Draw the compiled LangGraph coordinator from its own node and edge list."""
    graph = compiled.get_graph()
    x = {name: i * 2.25 for i, name in enumerate(PIPELINE_ORDER)}
    fig, ax = plt.subplots(figsize=(15, 3.4))
    for name in PIPELINE_ORDER:
        terminal = name.startswith("__")
        fan_out = name == "process_file"
        if fan_out:  # Stacked outlines: this node runs once per file, in parallel.
            for k in (2, 1):
                ax.add_patch(
                    FancyBboxPatch(
                        (x[name] - 0.95 + 0.08 * k, -0.75 - 0.08 * k),
                        1.9,
                        1.5,
                        boxstyle="round,pad=0.02,rounding_size=0.15",
                        facecolor="white",
                        edgecolor="#2a78d6",
                        linewidth=0.8,
                    )
                )
        ax.add_patch(
            FancyBboxPatch(
                (x[name] - 0.95, -0.75),
                1.9,
                1.5,
                boxstyle="round,pad=0.02,rounding_size=0.15",
                facecolor="#f3f2ef" if terminal else "#eef4fc",
                edgecolor="#2a78d6" if fan_out else GRID,
                linewidth=1.6 if fan_out else 1,
            )
        )
        ax.text(
            x[name], 0, PIPELINE_CAPTIONS[name], ha="center", va="center", fontsize=9.5, color=INK
        )
    for edge in graph.edges:
        a, b = x[edge.source], x[edge.target]
        if edge.source == "register" and edge.target == "equipment_registry":
            ax.annotate(
                "",
                xy=(b, 0.8),
                xytext=(a, 0.8),
                arrowprops=dict(
                    arrowstyle="->", color=MUTED, linestyle="--", connectionstyle="arc3,rad=-0.3"
                ),
            )
            ax.text(
                (a + b) / 2,
                1.55,
                "only if the folder is empty",
                ha="center",
                fontsize=8,
                color=MUTED,
            )
            continue
        ax.annotate(
            "",
            xy=(b - 0.97, 0),
            xytext=(a + 0.97, 0),
            arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.4),
        )
        if edge.conditional:
            ax.text(
                b,
                -1.15,
                "fan-out with Send: one worker per file",
                ha="center",
                fontsize=8,
                color="#2a78d6",
            )
    ax.set_xlim(-1.2, x["__end__"] + 1.2)
    ax.set_ylim(-1.4, 1.9)
    ax.axis("off")
    ax.set_title(
        "The construction coordinator, drawn from the compiled LangGraph",
        loc="left",
        fontsize=11,
        color=INK,
    )
    plt.show()


def node_label(node) -> str:
    p = node.properties
    if node.kind == "Document":
        return f"{p['source_id']} {p.get('role', '')}"
    if node.kind == "Event":
        if p["code"] == "UNVERIFIED_SUSPICION":
            return f"note {p['occurred_at'][11:16]}\n(suspicion)"
        if p["code"] == "HANDOVER_NOTE":
            return f"note {p['occurred_at'][11:16]}"
        return f"{p['event_id']}\n{p['code']}"
    if node.kind == "MaintenanceCase":
        return p["case_id"]
    if node.kind == "Finding":
        return f"{p['case_id']} finding ({p['status']})"
    if node.kind == "StockRecord":
        return f"stock {p['field_part_id']}\n{p['field_counted_at'][:10]}"
    if node.kind == "AlarmType":
        return p["code"]
    if node.kind == "ProcedureReference":
        return p["procedure_id"]
    if node.kind == "Part":
        return p["part_id"]
    return node.id


def to_networkx(bundle) -> nx.DiGraph:
    graph = nx.DiGraph()
    for node in bundle.nodes:
        graph.add_node(node.id, kind=node.kind, label=node_label(node))
    for edge in bundle.edges:
        graph.add_edge(edge.source, edge.target, relation=edge.relation)
    return graph


# Left to right: history | equipment | what happened and how it is set up | what applies | records.
COLUMNS = {
    "Finding": 0,
    "MaintenanceCase": 1,
    "Equipment": 2,
    "Event": 3,
    "Configuration": 3,
    "Document": 4,
    "Part": 4,
    "AlarmType": 4,
    "StockRecord": 5,
    "ProcedureReference": 5,
}
PLURALS = {
    "Finding": "findings",
    "MaintenanceCase": "maintenance cases",
    "Equipment": "equipment",
    "Event": "events",
    "Configuration": "configurations",
    "Document": "documents",
    "Part": "parts",
    "AlarmType": "alarms",
    "StockRecord": "stock records",
    "ProcedureReference": "procedures",
}


def layered_layout(graph: nx.DiGraph, sweeps: int = 8) -> dict:
    """One column per kind; within a column, order by the mean height of neighbours.

    Equipment is indented by depth in its component tree so the hierarchy reads at a glance.
    """
    depth = {}
    tree = graph.edge_subgraph(
        [(a, b) for a, b, r in graph.edges(data="relation") if r == "HAS_COMPONENT"]
    )
    for node in graph:
        if graph.nodes[node]["kind"] == "Equipment":
            depth[node] = len(nx.ancestors(tree, node)) if node in tree else 0
    column = {n: COLUMNS[graph.nodes[n]["kind"]] for n in graph}
    x = {n: column[n] * 3.0 + (0.55 * depth[n] if n in depth else 0) for n in graph}
    members = {}
    for n in sorted(graph, key=str):
        members.setdefault(column[n], []).append(n)
    y = {}
    for nodes in members.values():
        for i, n in enumerate(nodes):
            y[n] = -i
    undirected = graph.to_undirected()
    for sweep in range(sweeps):
        columns = sorted(members) if sweep % 2 == 0 else sorted(members, reverse=True)
        for c in columns:
            nodes = members[c]

            def centre(n):
                around = [y[m] for m in undirected[n] if column[m] != c] or [y[n]]
                return sum(around) / len(around)

            nodes.sort(key=centre, reverse=True)
            if c == COLUMNS["Equipment"]:  # Keep each parent directly above its components.
                nodes[:] = tree_order(nodes, tree)
            span = len(nodes) - 1
            for i, n in enumerate(nodes):
                y[n] = span / 2 - i
    return {n: (x[n], y[n]) for n in graph}


def tree_order(nodes: list, tree: nx.DiGraph) -> list:
    roots = [n for n in nodes if n not in tree or tree.in_degree(n) == 0]
    ordered = []

    def visit(n):
        ordered.append(n)
        for child in [c for c in nodes if n in tree and tree.has_edge(n, c)]:
            visit(child)

    for root in roots:
        visit(root)
    return ordered + [n for n in nodes if n not in ordered]


def draw_graph(
    graph: nx.DiGraph,
    title: str,
    edge_labels=False,
    size=(16, 10),
    font_size: int = 8,
    column_titles: bool = True,
):
    """Nodes coloured by group and always labelled, so identity never rests on colour.

    edge_labels: False, True, or a set of relation names to label.
    """
    pos = layered_layout(graph)
    fig, ax = plt.subplots(figsize=size)
    same_column = [(a, b) for a, b in graph.edges if pos[a][0] == pos[b][0]]
    other = [(a, b) for a, b in graph.edges if (a, b) not in same_column]
    nx.draw_networkx_edges(
        graph,
        pos,
        ax=ax,
        edgelist=other,
        edge_color="#c4c3bd",
        width=1.0,
        arrows=True,
        arrowsize=10,
        node_size=260,
    )
    nx.draw_networkx_edges(
        graph,
        pos,
        ax=ax,
        edgelist=same_column,
        edge_color="#c4c3bd",
        width=1.0,
        arrows=True,
        arrowsize=10,
        node_size=260,
        connectionstyle="arc3,rad=0.35",
    )
    colours = [GROUPS[graph.nodes[n]["kind"]][1] for n in graph.nodes]
    nx.draw_networkx_nodes(
        graph, pos, ax=ax, node_color=colours, node_size=260, edgecolors="white", linewidths=1.5
    )
    for n, (px, py) in pos.items():
        ax.text(
            px + 0.14,
            py,
            graph.nodes[n]["label"].replace("\n", " · "),
            fontsize=font_size,
            color=INK,
            va="center",
            ha="left",
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=0.4),
        )
    if edge_labels:
        wanted = {
            (a, b): r
            for a, b, r in graph.edges(data="relation")
            if edge_labels is True or r in edge_labels
        }
        nx.draw_networkx_edge_labels(
            graph,
            pos,
            ax=ax,
            edge_labels=wanted,
            font_size=6.5,
            font_color=MUTED,
            rotate=False,
            label_pos=0.45,
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.9, pad=0.2),
        )
    top = max(py for _, py in pos.values()) + 1.1
    if column_titles:
        kinds = {graph.nodes[n]["kind"] for n in graph}
        for c in sorted({COLUMNS[k] for k in kinds}):
            names = [PLURALS[k] for k in COLUMNS if COLUMNS[k] == c and k in kinds]
            ax.text(
                c * 3.0,
                top,
                " · ".join(names).upper(),
                fontsize=8,
                color=MUTED,
                ha="left",
                va="bottom",
            )
    order = list(dict.fromkeys(GROUPS.values()))
    present = {GROUPS[graph.nodes[n]["kind"]] for n in graph.nodes}
    ax.legend(
        handles=[
            Line2D([], [], marker="o", linestyle="", markersize=9, color=colour, label=name)
            for name, colour in order
            if (name, colour) in present
        ],
        loc="upper left",
        bbox_to_anchor=(0.0, -0.01),
        ncol=4,
        frameon=False,
        fontsize=9,
    )
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=14)
    xs = [px for px, _ in pos.values()]
    ax.set_xlim(min(xs) - 0.4, max(xs) + 2.6)
    ax.set_ylim(min(py for _, py in pos.values()) - 0.8, top + 0.6)
    ax.axis("off")
    plt.tight_layout()
    plt.show()


def subgraph(bundle, relations: set[str]) -> nx.DiGraph:
    """Only the links of the given kinds, with the nodes they join."""
    nodes = {n.id: n for n in bundle.nodes}
    graph = nx.DiGraph()
    for edge in bundle.edges:
        if edge.relation in relations:
            for end in (edge.source, edge.target):
                graph.add_node(end, kind=nodes[end].kind, label=node_label(nodes[end]))
            graph.add_edge(edge.source, edge.target, relation=edge.relation)
    return graph


def neighbourhood(graph: nx.DiGraph, centre: str, hops: int, keep=None) -> nx.DiGraph:
    reach = nx.single_source_shortest_path_length(graph.to_undirected(), centre, cutoff=hops)
    return graph.subgraph([n for n in reach if keep is None or keep(n)]).copy()


# Tables -------------------------------------------------------------------------------


def trace_table(result) -> pd.DataFrame:
    """One row per step a file went through: which agent decided, and what."""
    rows = []
    for step in result.trace:
        d = step.get("decision", {})
        stage = step["stage"]
        if stage == "triage":
            summary = f"{d['doc_type']} (second choice {d['alternative']})"
        elif stage == "extract":
            summary = f"read as {step['doc_type']}"
        elif stage == "correct":
            summary = (
                f"{len(step['feedback'])} failed checks returned; {len(step['remaining'])} remain"
            )
        elif stage == "reroute":
            summary = f"{step['from']} → {step['to']}, kept: {step['kept']}"
        elif stage == "reconcile":
            summary = f"{len(d['decisions'])} mention(s)"
        else:
            summary = ""
        rows.append({"stage": stage, "agent": step.get("agent", "code"), "result": summary})
    return pd.DataFrame(rows)


def stock_table(bundle) -> pd.DataFrame:
    rows = []
    for n in bundle.nodes:
        if n.kind != "StockRecord":
            continue
        p = n.properties
        rows.append(
            {
                "part": p["field_part_id"],
                "location": p["field_location"],
                "counted_at": p["field_counted_at"][:16],
                "on hand": p.get("field_quantity", p.get("field_quantity_on_hand")),
                "reserved": p.get("field_quantity_reserved", "not recorded"),
                "schema": p["schema_version"],
            }
        )
    return pd.DataFrame(sorted(rows, key=lambda r: (r["part"], r["counted_at"])))


def bar_counts(counts: dict, title: str, colour: str = "#2a78d6"):
    """Horizontal bars, largest first, each labelled with its value."""
    items = sorted(counts.items(), key=lambda kv: kv[1])
    fig, ax = plt.subplots(figsize=(6.5, 0.34 * len(items) + 0.8))
    bars = ax.barh([k for k, _ in items], [v for _, v in items], color=colour, height=0.6)
    for bar, (_, value) in zip(bars, items):
        ax.text(
            bar.get_width() + 0.2,
            bar.get_y() + bar.get_height() / 2,
            str(value),
            va="center",
            fontsize=9,
            color=INK,
        )
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    ax.tick_params(axis="y", colors=INK, length=0)
    plt.tight_layout()
    plt.show()
