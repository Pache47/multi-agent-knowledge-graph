"""Sidebar settings shared by chat and equipment views: line, records cutoff and mode."""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from factory_assistant.scenarios import select_scenario

LINES = {"main": "Packaging line 1 — outfeed", "sensor_stop": "Carton buffer — detector stops"}
PLANT = ZoneInfo("Asia/Singapore")


def readable_time(value: str) -> str:
    """Plant time with an explicit offset; invalid text is shown as typed."""
    try:
        return datetime.fromisoformat(value).astimezone(PLANT).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return value


def pick_time(cutoff: str, key: str) -> str:
    """Any earlier or later moment, picked as a plant date and time instead of typed."""
    default = datetime.fromisoformat(cutoff)
    picked_day = st.date_input(
        "Records up to",
        default.date(),
        key=key + "-day",
        help=(
            "Answer as of any moment in plant time, for example the day before the incident. "
            "Files added after it, and events and stock counts recorded after it, are left out."
        ),
    )
    picked_time = st.time_input(
        "at", default.time().replace(second=0, microsecond=0), step=60, key=key + "-time"
    )
    picked = datetime.combine(picked_day, picked_time, default.tzinfo)
    # The pickers show whole minutes; an unchanged choice keeps the exact preset time.
    return cutoff if picked == default.replace(second=0, microsecond=0) else picked.isoformat()


def render_settings(root: Path, runtime: Path) -> dict:
    """Plain choices first; developer options stay in an expander."""
    with st.sidebar:
        st.header("Settings")
        line = st.selectbox(
            "Production line",
            list(LINES),
            format_func=LINES.get,
            key="line",
            help="Each line has its own folder of files and its own knowledge graph.",
        )
        try:
            metadata, bundle = select_scenario(root, runtime, line)
        except ValueError:
            st.warning("This line's knowledge is not built yet. Use **Build & review → Build**.")
            return {"line": line, "bundle": None}
        incident, latest = metadata["question_at"], metadata["latest_question_at"]
        window = st.radio(
            "Records to use",
            ["incident", "latest"],
            key=f"window-{line}",
            format_func=lambda v: (
                f"At the incident ({readable_time(incident)})"
                if v == "incident"
                else f"All records ({readable_time(latest)})"
            ),
            help=(
                "At the incident shows only what a technician could have known then: files "
                "added later are hidden. All records includes everything, up to the newest file."
            ),
        )
        cutoff = incident if window == "incident" else latest
        has_key = bool(os.getenv("OPENAI_API_KEY"))
        mode = st.radio(
            "Assistant",
            ["live", "offline"],
            index=0 if has_key else 1,
            key="mode",
            format_func=lambda v: "Live AI answers" if v == "live" else "Evidence preview only",
            help=(
                "Live AI answers explain the records and are checked by a second model; the "
                "question and fictional evidence go to OpenAI. Evidence preview only lists the "
                "matching records, with no model calls."
            ),
        )
        if mode == "live" and not has_key:
            st.error("No OPENAI_API_KEY is configured; live answers will fail.")
        with st.expander("Advanced"):
            custom = pick_time(cutoff, f"cutoff-{line}-{window}")
            search = st.radio(
                "Passage search",
                ["hybrid", "keyword"],
                key="search",
                disabled=mode == "offline",
                format_func=lambda v: "Meaning + words" if v == "hybrid" else "Words only",
                help=(
                    "How manuals and notes are searched. Meaning + words also finds passages "
                    "that say the same thing in other words; words only matches the exact terms."
                ),
            )
        st.caption(
            f"Knowledge build {bundle.fingerprint[:8]} · {len(bundle.nodes)} records. "
            "Records are dated snapshots, not live machine status."
        )
    return {
        "line": line,
        "metadata": metadata,
        "bundle": bundle,
        "cutoff": custom,
        "mode": mode,
        "search": search if mode == "live" else "keyword",
    }
