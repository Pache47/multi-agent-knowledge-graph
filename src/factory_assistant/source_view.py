"""A cited file on its own page: the cited lines first, then the whole text as the agents read it.

Answers link here (`?view=<source id>&set=<line>&lines=4,5`) so a technician can check a
citation in context. Only files registered in the selected knowledge can be opened.
"""

import hashlib
from pathlib import Path
from urllib.parse import urlencode

import streamlit as st

from factory_assistant.scenarios import select_scenario
from factory_assistant.settings_ui import LINES, readable_time


def source_link(source_id: str, line: str, lines) -> str:
    return "?" + urlencode(
        {"view": source_id, "set": line, "lines": ",".join(str(n) for n in sorted(lines))}
    )


def original_file(root: Path, runtime: Path, path: str) -> Path | None:
    """Registered files live in data/; files added in the app live in the runtime folder."""
    for base in (root / "data", runtime / "uploads"):
        candidate = (base.parent / path).resolve()
        if candidate.is_relative_to(base.resolve()) and candidate.is_file():
            return candidate
    return None


def render_source(root: Path, runtime: Path, params) -> None:
    line = params.get("set", "main")
    if line not in LINES:
        st.error("Unknown production line.")
        return
    _, bundle = select_scenario(root, runtime, line)
    node = next((n for n in bundle.nodes if n.id == f"document:{params.get('view')}"), None)
    if node is None:
        st.error("This file is not in the current knowledge build.")
        return
    text = node.properties["text"].splitlines()
    cited = sorted(
        {
            int(n)
            for n in params.get("lines", "").split(",")
            if n.isdigit() and 0 < int(n) <= len(text)
        }
    )
    st.title(f"📄 {Path(node.properties['path']).name}")
    st.caption(
        f"{LINES[line]} · added to the folder {readable_time(node.properties['recorded_at'])}"
        f" · recognised as {node.properties.get('role', '—').replace('_', ' ')}"
    )
    if cited:
        st.markdown("**Cited lines**")
        st.code(
            "\n".join(f"line {n:>3}  {text[n - 1]}" for n in cited), language=None, wrap_lines=True
        )
    st.markdown("**Whole file**, as the agents read it (line numbers match the citations)")
    st.code("\n".join(text), language=None, line_numbers=True, wrap_lines=True)
    original = original_file(root, runtime, node.properties["path"])
    data = original.read_bytes() if original else b""
    if original and hashlib.sha256(data).hexdigest() == node.properties["sha256"]:
        st.download_button("Download the original file", data, file_name=original.name)
    elif original:
        st.caption("The file has changed since this build; rebuild to cite the new text.")
