"""Files added through the app, kept apart from the customer's registered folder.

An added file is stored in the runtime folder with its own registration time, so the
committed data, its manifest and the gold checks never change. Each build reads the
registered folder plus these files; removing one removes its facts on the next build.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from filelock import FileLock

SUFFIXES = {".txt", ".md", ".csv", ".log", ".pdf"}
FOLDER = "uploads"


def upload_dir(runtime: Path) -> Path:
    return runtime / FOLDER


def added_files(runtime: Path) -> list[dict]:
    path = upload_dir(runtime) / "manifest.json"
    if not path.exists():
        return []
    return [
        {**entry, "location": str(upload_dir(runtime) / entry["path"])}
        for entry in json.loads(path.read_text())
    ]


def _save(runtime: Path, entries: list[dict]):
    stored = [{k: v for k, v in e.items() if k != "location"} for e in entries]
    (upload_dir(runtime) / "manifest.json").write_text(json.dumps(stored, indent=2))


def add_file(runtime: Path, name: str, data: bytes, recorded_at: str) -> dict:
    """Store one file under a new source ID; the name is kept for people, never for routing."""
    name = Path(name).name
    if Path(name).suffix.lower() not in SUFFIXES or not re.fullmatch(r"[\w .()-]{1,120}", name):
        raise ValueError("Add a .txt, .md, .csv, .log or .pdf file with a plain file name.")
    if datetime.fromisoformat(recorded_at).utcoffset() is None:
        raise ValueError("The registration time needs a timezone.")
    folder = upload_dir(runtime)
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(folder / ".lock")):
        entries = added_files(runtime)
        number = max((int(e["id"][1:]) for e in entries), default=0) + 1
        identity = f"U{number}"
        (folder / identity).mkdir()
        (folder / identity / name).write_bytes(data)
        entry = {
            "id": identity,
            "file": name,
            "recorded_at": recorded_at,
            "path": f"{identity}/{name}",
        }
        _save(runtime, [*entries, entry])
    return {**entry, "location": str(folder / entry["path"])}


def remove_file(runtime: Path, source_id: str):
    folder = upload_dir(runtime)
    with FileLock(str(folder / ".lock")):
        entries = added_files(runtime)
        removed = [e for e in entries if e["id"] == source_id]
        if not removed:
            raise ValueError(f"No added file {source_id}.")
        _save(runtime, [e for e in entries if e["id"] != source_id])
        file = Path(removed[0]["location"])
        file.unlink(missing_ok=True)
        file.parent.rmdir()
