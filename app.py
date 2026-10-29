"""Factory assistant: ask about a stop, look up a machine, or build and review the knowledge."""

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from factory_assistant.answer_ui import render_chat
from factory_assistant.construction_ui import render_construction
from factory_assistant.equipment_ui import render_equipment
from factory_assistant.settings_ui import render_settings
from factory_assistant.source_view import render_source

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
RUNTIME = Path(os.environ.get("FACTORY_RUNTIME", ROOT / ".runtime"))

st.set_page_config(page_title="Factory assistant", page_icon="🏭", layout="wide")
# A cited file opens on its own page (answers link to it in a new tab).
if "view" in st.query_params:
    render_source(ROOT, RUNTIME, st.query_params)
    st.stop()
st.title("🏭 Factory assistant")
st.caption("Troubleshooting help from your line's own records · fictional demonstration data")
# Fixed tab labels: a label that changes during a build would switch the open tab.
ask, equipment, build = st.tabs(
    ["💬 Ask about a problem", "🔧 Equipment", "🏗️ Build & review"], key="tabs"
)
# Build first, so a newly saved graph is what the other views read on this same run.
# No view starts a model call without an explicit action.
with build:
    render_construction(ROOT, RUNTIME)
settings = render_settings(ROOT, RUNTIME)
with ask:
    _, middle, _ = st.columns([1, 5, 1])
    with middle:
        render_chat(ROOT, RUNTIME, settings)
with equipment:
    render_equipment(settings)
