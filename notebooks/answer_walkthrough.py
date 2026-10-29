"""Helpers for the answering walkthroughs (concise and detailed) and the evaluation notebook.

Two parts:
- `RecordedAssistant`: the live assistant's interface with recorded model outputs, so the
  notebooks replay without an API key (like `agent_cache/` does for construction).
- Presentation only: tables, an answer view and a figure. Every assistant step is called
  directly in the notebook from `factory_assistant`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import Markdown, display
from kg_walkthrough import GRID, INK, MUTED, short
from matplotlib.patches import FancyBboxPatch

from factory_assistant.assistant import QUERY_INSTRUCTIONS, LiveAssistant

SECTION_TITLES = {
    "findings": "What the records show",
    "possible_causes": "Possible causes to check",
    "next_steps": "Next steps",
}


# Recorded model outputs ---------------------------------------------------------------


class _Item(SimpleNamespace):
    """A recorded output item with the attributes and model_dump() the tool loop uses."""

    def model_dump(self, **_):
        return dict(self.__dict__)


class RecordedAssistant(LiveAssistant):
    """LiveAssistant with every model output recorded under a hash of its exact input.

    mode="replay": recorded outputs only; a new input raises KeyError (no API key needed).
    mode="live":   recorded outputs where they exist; new inputs call the model and are recorded.
    Graph tools, code checks and release always run for real.
    """

    def __init__(self, path: Path, mode: str = "replay", query_instructions=QUERY_INSTRUCTIONS):
        # LiveAssistant.__init__ is skipped on purpose: a client is created only on a miss.
        self.path, self.mode = Path(path), mode
        self.query_instructions = query_instructions
        self.max_draft_tokens = 2200
        self.store = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.model = self.store.get("_model", os.getenv("OPENAI_MODEL") or "gpt-4.1-mini")
        self.verifier_model = self.model
        self.requests, self.calls = [], []
        self._live, self._lock = None, threading.Lock()

    def live(self) -> LiveAssistant:
        if self.mode != "live":
            raise KeyError("Not recorded. Set MODE = 'live' (needs OPENAI_API_KEY) to run it.")
        if self._live is None:
            self._live = LiveAssistant(query_instructions=self.query_instructions)
            self.store["_model"] = self.model = self.verifier_model = self._live.model
        return self._live

    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[
            :24
        ]

    def _record(self, key, stage, output, usage):
        with self._lock:
            self.store[key] = {"stage": stage, "output": output, "usage": usage}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.store, indent=1, sort_keys=True))

    def _usage(self, response):
        return response.usage.model_dump() if response.usage else None

    def choose_tool(self, conversation: list, definitions: list[dict]):
        key = self.key("tool", self.query_instructions, conversation, definitions)
        if key not in self.store:
            response = self.live().choose_tool(conversation, definitions)
            items = [item.model_dump(exclude_none=True) for item in response.output]
            self._record(key, "investigator", items, self._usage(response))
        entry = self.store[key]
        self.calls.append({"stage": "investigator", "usage": entry["usage"]})
        return SimpleNamespace(output=[_Item(**item) for item in entry["output"]])

    def structured(self, instructions: str, context: dict, model, *, reviewing: bool = False):
        key = self.key("structured", instructions, context, model.model_json_schema(), reviewing)
        stage = "verifier" if reviewing else "responder"
        if key not in self.store:
            client = self.live()
            parsed = client.structured(instructions, context, model, reviewing=reviewing)
            usage = client.requests[-1]["usage"] if client.requests else None
            self._record(key, stage, parsed.model_dump(), usage)
        entry = self.store[key]
        self.calls.append({"stage": stage, "usage": entry["usage"]})
        return model.model_validate(entry["output"])

    def call_summary(self) -> pd.DataFrame:
        """Model calls made for the last run(s), by agent, with recorded token use."""
        rows = {}
        for call in self.calls:
            row = rows.setdefault(call["stage"], {"agent": call["stage"], "calls": 0, "tokens": 0})
            row["calls"] += 1
            row["tokens"] += (call["usage"] or {}).get("total_tokens", 0)
        return pd.DataFrame(rows.values())


# Tables -------------------------------------------------------------------------------


def tool_table(definitions: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "tool": d["name"],
                "arguments": ", ".join(d["parameters"].get("properties", {})),
                "what it does": short(" ".join(d["description"].split()).split(". ")[0], 105),
            }
            for d in definitions
        ]
    )


def result_summary(tool: str, result: dict | None) -> str:
    """One line per tool result, for the investigator trace."""
    if result is None:
        return ""
    if "error" in result:
        return "error: " + short(result["error"], 80)
    if tool == "find_equipment":
        ids = ", ".join(m["node_id"] for m in result.get("matches", []))
        return ("resolved " if result.get("resolved") else "not resolved; candidates ") + ids
    if tool == "troubleshooting_context":
        r, events = result, result["recorded_events"]
        return (
            f"{len(r['applicable_manuals'])} manual, {len(r['manual_sections'])} sections, "
            f"{events.get('count')} events, {len(r['shift_observations'])} shift notes, "
            f"{len(r['maintenance_history'])} past cases, {len(r['procedures'])} procedure"
        )
    if tool == "query_stock":
        approvals, stock = result.get("approvals", []), result.get("stock", [])
        return f"{len(approvals)} approvals, {len(stock)} stock records"
    if tool == "approved_parts":
        return f"{len(result.get('parts', []))} approved parts"
    if tool == "query_history":
        return f"{len(result.get('cases', []))} cases"
    if "results" in result:
        return f"{len(result['results'])} passages"
    if "paths" in result:
        return f"{len(result['paths'])} paths"
    if "count" in result:
        return f"count {result['count']}"
    return short(json.dumps(result), 80)


def investigation_table(result) -> pd.DataFrame:
    rows = []
    for step in result.trace:
        if "tool" in step:
            rows.append(
                {
                    "step": len(rows) + 1,
                    "tool": step["tool"],
                    "arguments": short(json.dumps(step["arguments"]), 70),
                    "returned": result_summary(step["tool"], step.get("result")),
                }
            )
    return pd.DataFrame(rows)


def _props(card: dict) -> dict:
    return json.loads(card["text"])["properties"]


def context_table(ctx: dict, cards: dict) -> pd.DataFrame:
    """What one troubleshooting_context call gathered, and from which files."""

    def files(*keys):
        names = {r.path.rsplit("/", 1)[-1] for k in keys for r in cards[k].evidence}
        return ", ".join(sorted(names))

    manuals = ctx["applicable_manuals"]
    events = ctx["recorded_events"]
    notes = ctx["shift_observations"]
    history = ctx["maintenance_history"]
    rows = [
        (
            "machine",
            f"{ctx['equipment']['node_id']}, aliases {_props(ctx['equipment'])['aliases']}",
            files(ctx["equipment"]["id"]),
        ),
        (
            "applicable manual",
            ", ".join(
                f"{_props(m['manual'])['document_id']} rev {_props(m['manual'])['revision']} "
                f"(via configuration {_props(m['manual'])['configuration']})"
                for m in manuals
            ),
            files(*[m["applicability_path"] for m in manuals]),
        ),
        (
            "manual sections",
            ", ".join(s["title"] for s in ctx["manual_sections"]),
            files(*[s["id"] for s in ctx["manual_sections"]]),
        ),
        (
            "recorded events",
            ", ".join(f"{k} × {v}" for k, v in events.get("by_code", {}).items()),
            files(*[e["id"] for e in events.get("events", [])]),
        ),
        (
            "alarm definitions",
            "; ".join(
                f"{_props(a)['code']}: {short(_props(a).get('meaning', ''), 60)}"
                for a in ctx["alarm_definitions"]
            ),
            files(*[a["id"] for a in ctx["alarm_definitions"]]),
        ),
        (
            "shift observations",
            f"{len(notes)} notes, of which "
            f"{sum(n['kind'] == 'unverified suspicion' for n in notes)} unverified suspicion",
            files(*[n["card"]["id"] for n in notes]),
        ),
        (
            "past cases",
            ", ".join(
                f"{h['record_summary_id'].split(':')[-1]} (setup then: "
                f"{h['configuration_comparison'].replace('_', ' ')})"
                for h in history
            ),
            files(
                *[
                    k
                    for h in history
                    if (k := "node:" + h["record_summary_id"].removeprefix("history:")) in cards
                ]
            ),
        ),
        (
            "procedures",
            ", ".join(
                f"{_props(p)['procedure_id']} (full text in folder: "
                f"{_props(p).get('details_available')})"
                for p in ctx["procedures"]
            ),
            files(*[p["id"] for p in ctx["procedures"]]),
        ),
    ]
    return pd.DataFrame(rows, columns=["context", "found", "from files"])


def card_sources(card) -> str:
    """Compact 'file:lines' for an evidence card."""
    by_file = {}
    for ref in card.evidence:
        by_file.setdefault(ref.path.rsplit("/", 1)[-1], []).append(str(ref.line))
    return "; ".join(f"{f}:{','.join(lines)}" for f, lines in by_file.items()) or "technician"


def draft_table(draft, cards: dict) -> pd.DataFrame:
    rows = [
        {
            "section": section,
            "claim": claim.text,
            "cites": " + ".join(card_sources(cards[c]) for c in claim.citations if c in cards),
        }
        for section in SECTION_TITLES
        for claim in getattr(draft, section)
    ]
    if draft.question:
        rows.append({"section": "question", "claim": draft.question, "cites": ""})
    rows += [{"section": "record (code)", "claim": f, "cites": ""} for f in draft.fact_ids]
    return pd.DataFrame(rows)


def checks_table(result) -> pd.DataFrame:
    """Each draft attempt and what code and the verifier said about it."""
    rows = []
    for step in result.trace:
        if step.get("stage") == "verify":
            draft = step["draft"]
            claims = sum(len(draft[s]) for s in SECTION_TITLES)
            rows.append(
                {
                    "attempt": step["attempt"],
                    "claims": claims,
                    "issues found": len(step["issues"]),
                    "details": " | ".join(short(i, 110) for i in step["issues"]) or "all supported",
                }
            )
    return pd.DataFrame(rows)


def sources_table(result) -> pd.DataFrame:
    """Every released statement with the files and lines behind it."""
    cards = {c.id: c for c in result.evidence}
    rows = []
    for section in SECTION_TITLES if result.response else ():
        for claim in getattr(result.response, section):
            lines = {}
            for key in claim.citations:
                for ref in cards[key].evidence if key in cards else []:
                    lines.setdefault(ref.path.rsplit("/", 1)[-1], set()).add(ref.line)
            sources = "; ".join(
                f"{name} l.{','.join(map(str, sorted(nums)))}" for name, nums in lines.items()
            )
            rows.append(
                {
                    "section": section,
                    "statement": short(claim.text, 80),
                    "sources": sources or "technician's report",
                }
            )
    return pd.DataFrame(rows)


def claim_lines(result, section: str = "possible_causes", index: int = 0) -> list:
    """The exact source lines behind one released statement."""
    cards = {c.id: c for c in result.evidence}
    claim = getattr(result.response, section)[index]
    refs = {(r.source_id, r.line): r for key in claim.citations for r in cards[key].evidence}
    return claim.text, list(refs.values())


# Answer view --------------------------------------------------------------------------


def show_answer(result):
    """The answer as the technician sees it: claims, code-rendered records, question."""
    lines = [f"**Status:** `{result.status}`" + (f" · {result.message}" if result.message else "")]
    response = result.response
    shown = set(response.fact_ids) if response else {f.id for f in result.facts}
    if response:
        for section, title in SECTION_TITLES.items():
            claims = getattr(response, section)
            if claims:
                lines.append(f"\n**{title}**")
                lines += [f"- {c.text}" for c in claims]
    records = [f for f in result.facts if f.id in shown]
    if records:
        lines.append("\n**Records, rendered by code** (never rewritten by a model)")
        for f in records:
            label = f.kind.replace("_", " ")
            lines.append(f"- *{label}:* " + f.text.replace("\n", " · "))
    if response and response.question:
        lines.append(f"\n**Question for you:** {response.question}")
    display(Markdown("\n".join(lines)))


# Figure -------------------------------------------------------------------------------

FLOW = [
    ("question", "question\n+ records time", "#f3f2ef"),
    ("investigator", "Investigator\nagent\nread-only graph tools", "#eef4fc"),
    ("responder", "Responder\nagent\ncited, sectioned draft", "#eef4fc"),
    ("checks", "Code checks\ncitations · sections\n· records", "#f3f2ef"),
    ("verifier", "Verifier\nagent\neach claim alone", "#eef4fc"),
    ("release", "Release (code)\nverified claims\n+ records + safety", "#f3f2ef"),
]


def draw_answer_flow():
    """The answering path: agents (blue) and code (grey), with its two side exits."""
    fig, ax = plt.subplots(figsize=(15, 3.6))
    x = {name: i * 2.4 for i, (name, _, _) in enumerate(FLOW)}
    for name, caption, colour in FLOW:
        agent = colour == "#eef4fc"
        ax.add_patch(
            FancyBboxPatch(
                (x[name] - 1.0, -0.75),
                2.0,
                1.5,
                boxstyle="round,pad=0.02,rounding_size=0.15",
                facecolor=colour,
                edgecolor="#2a78d6" if agent else GRID,
                linewidth=1.4 if agent else 1,
            )
        )
        ax.text(x[name], 0, caption, ha="center", va="center", fontsize=9.5, color=INK)
    for (a, _, _), (b, _, _) in zip(FLOW, FLOW[1:]):
        ax.annotate(
            "",
            xy=(x[b] - 1.02, 0),
            xytext=(x[a] + 1.02, 0),
            arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.4),
        )
    # One correction: verifier failures go back to the responder once.
    ax.annotate(
        "",
        xy=(x["responder"], 0.8),
        xytext=(x["verifier"], 0.8),
        arrowprops=dict(arrowstyle="->", color="#eb6834", connectionstyle="arc3,rad=0.35"),
    )
    ax.text(
        (x["responder"] + x["verifier"]) / 2,
        1.95,
        "failures go back once",
        ha="center",
        fontsize=8.5,
        color="#eb6834",
    )
    side = [
        ("investigator", "ambiguous machine →\nneutral question\nbuilt by code"),
        ("responder", "graph found nothing →\none bounded\nsource-text search"),
    ]
    for name, text in side:
        ax.text(x[name], -1.0, text, ha="center", va="top", fontsize=8.3, color=MUTED)
    ax.set_xlim(-1.3, x["release"] + 1.3)
    ax.set_ylim(-2.2, 2.3)
    ax.axis("off")
    ax.set_title(
        "Answering a question: agents (blue) gather, write and check; code (grey) enforces",
        loc="left",
        fontsize=11,
        color=INK,
    )
    plt.show()


def touched_nodes(tools) -> set[str]:
    """Graph nodes whose cards a run collected (what the answer could draw on), plus the
    nodes that connect them (such as the drive its events are recorded for)."""
    nodes = {key.removeprefix("node:") for key in tools.cards if key.startswith("node:")}
    links = {}
    for e in tools.edges:
        for inside, outside in ((e.source, e.target), (e.target, e.source)):
            if inside in nodes and outside not in nodes:
                links[outside] = links.get(outside, 0) + 1
    return nodes | {n for n, count in links.items() if count >= 2}
