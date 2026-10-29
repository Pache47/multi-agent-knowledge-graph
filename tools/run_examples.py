"""Ask the live assistant a fixed set of technician questions and save readable answers.

Usage: PYTHONPATH=src python tools/run_examples.py OUTPUT.json   (spends API credits)
These are development examples for inspecting behaviour, not a scored evaluation.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

from factory_assistant.answer_ui import conversation_context
from factory_assistant.assistant import answer_question
from factory_assistant.pipeline import build_knowledge
from factory_assistant.scenarios import load_scenario

root = Path(__file__).resolve().parents[1]
load_dotenv(root / ".env")
out = Path(sys.argv[1])
runtime = Path(tempfile.mkdtemp())
main = build_knowledge(root, runtime)
INCIDENT, LATEST = "2026-10-14T08:20:00+08:00", "2026-10-18T08:00:00+08:00"
PRIMARY = "The outfeed belt stopped twice after the carton changeover. The drive says motor overload. What should I check next?"
FOLLOW = (
    "After isolation and inspection under our procedure we found a roller underneath the belt "
    "that barely turns. Cartons and side guides were clear. Which part do we need, and is one in stock?"
)
cases = [
    ("troubleshoot", "main", INCIDENT, [], PRIMARY),
    ("follow_up", "main", INCIDENT, [PRIMARY], FOLLOW),
    ("manual", "main", INCIDENT, [], "Which manual applies to CV-01, and why?"),
    (
        "history",
        "main",
        LATEST,
        [],
        "What maintenance has been recorded on the outfeed belt before?",
    ),
    ("parts", "main", INCIDENT, [], "Can RA-200A be used instead of RA-200B on CV-01?"),
    ("ambiguous", "main", INCIDENT, [], "The outfeed stopped. What should I check?"),
    (
        "sensor",
        "sensor_stop",
        "2026-10-17T14:10:00+08:00",
        [],
        "The carton buffer stopped and shows SENSOR_BLOCKED. What do the records suggest?",
    ),
]
results = []
for name, scenario, as_of, earlier, question in cases:
    if scenario == "main":
        bundle, dataset = main, "initial-incident"
    else:
        meta, bundle = load_scenario(root, scenario)
        dataset = meta["dataset"]
    turns = [{"message": m} for m in earlier]
    start = time.time()
    r = answer_question(
        bundle, conversation_context(turns, question), as_of, dataset, runtime, mode="live"
    )
    seconds = round(time.time() - start, 1)
    lines = [f"## {name} · {r.status} · {seconds}s", f"Q: {question}"]
    if r.message:
        lines.append(f"message: {r.message}")
    resp = r.response
    shown = (
        set(resp.fact_ids)
        if resp
        else {f.id for f in r.facts}
        if r.status == "evidence_only"
        else set()
    )
    for f in r.facts:
        if f.id in shown:
            lines.append(f"[{f.kind}] " + f.text.replace("\n", " / "))
    if resp:
        for section in ("findings", "possible_causes", "next_steps"):
            for c in getattr(resp, section):
                lines.append(f"{section}: {c.text}")
        if resp.question:
            lines.append(f"question: {resp.question}")
    tools = [t["tool"] for t in r.trace if t.get("tool")]
    attempts = [t for t in r.trace if t.get("stage") == "verify"]
    lines.append(f"tools: {tools} · draft attempts: {len(attempts)}")
    for a in attempts:
        if a["issues"]:
            lines.append("  rejected: " + " | ".join(a["issues"])[:600])
    print("\n".join(lines) + "\n", flush=True)
    results.append(
        {
            "case": name,
            "question": question,
            "earlier": earlier,
            "as_of": as_of,
            "seconds": seconds,
            "result": r.model_dump(),
        }
    )
out.write_text(json.dumps(results, indent=1))
