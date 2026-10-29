"""Source manifests, line citations and the checks that make model quotes trustworthy.

Agents never write evidence. They point at text; code finds that text in the source,
records its exact lines (and PDF pages), and rejects anything it cannot find.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from factory_assistant.graph_models import Evidence, Node
from factory_assistant.models import SourceSnapshot
from factory_assistant.source_readers import read_source

MAIN_FOLDER = "data/shared_folder"


def load_manifest(root: Path, folder: str = MAIN_FOLDER) -> dict:
    """A manifest registers files and their dates. It says nothing about their contents."""
    base = (root / "data").resolve()
    path = (root / folder).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Source folders must stay inside data/.")
    manifest = json.loads((path / "manifest.json").read_text())
    ZoneInfo(manifest["timezone"])
    ids = [entry["id"] for entry in manifest["sources"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source ID in the manifest.")
    manifest["sources"] = [
        {**entry, "path": str((path / entry["file"]).relative_to(root.resolve()))}
        for entry in manifest["sources"]
    ]
    return manifest


def read_registered(
    root: Path, manifest: dict, added: Path | None = None
) -> tuple[list[SourceSnapshot], list[dict]]:
    sources, held = [], []
    for entry in manifest["sources"]:
        try:
            sources.append(read_source(root, entry, added if entry.get("added") else None))
        except (ValueError, OSError) as exc:
            held.append({"source": entry["id"], "stage": "read", "reason": str(exc)})
    return sources, held


def numbered(source: SourceSnapshot) -> str:
    """The text agents see: every line keeps the number that citations will use."""
    return "\n".join(f"{n}: {line}" for n, line in enumerate(source.text.splitlines(), 1))


def evidence_at(source: SourceSnapshot, numbers: list[int]) -> list[Evidence]:
    lines = source.text.splitlines()
    if not numbers or any(number < 1 or number > len(lines) for number in numbers):
        raise ValueError("Missing or out-of-range source reference.")
    return [
        Evidence(
            source_id=source.source_id,
            path=source.path,
            sha256=source.sha256,
            line=n,
            excerpt=lines[n - 1],
            page=source.page_numbers[n - 1] if source.page_numbers else None,
        )
        for n in sorted(set(numbers))
    ]


def squash(text: str) -> str:
    return " ".join(text.split())


def locate(source: SourceSnapshot, quote) -> tuple[list[Evidence], str]:
    """Find a quote that starts on its cited line; it may run onto the following lines.

    Whitespace and letter case are normalised (PDF extraction and line wrapping vary);
    wording and punctuation must match, so a paraphrase cannot pass as a citation.
    Returns the cited lines and the text as the source writes it.
    """
    lines = source.text.splitlines()
    target = squash(quote.text).lower()
    if not target:
        raise ValueError("Empty quote.")
    if not 1 <= quote.line <= len(lines) or not lines[quote.line - 1].strip():
        raise ValueError(f"{quote.text!r} cites line {quote.line}, which has no text.")
    joined, spans = "", []
    for number in range(quote.line, min(len(lines), quote.line + 8) + 1):
        piece = squash(lines[number - 1])
        if not piece:
            continue
        start = len(joined) + 1 if joined else 0
        joined = f"{joined} {piece}" if joined else piece
        spans.append((number, start, len(joined)))
        index = find_quote(joined, squash(quote.text))
        if index != -1 and index < spans[0][2]:
            end = index + len(target)
            refs = evidence_at(source, [n for n, s, e in spans if s < end and e > index])
            return refs, joined[index:end]
    raise ValueError(f"{quote.text!r} was not found starting on line {quote.line}.")


def find_quote(text: str, target: str) -> int:
    """Prefer an exact-case, whole-word match: 'B' must not match the b in 'belt'."""
    for haystack, needle in ((text, target), (text.lower(), target.lower())):
        for match in re.finditer(re.escape(needle), haystack):
            before = haystack[match.start() - 1] if match.start() else " "
            after = haystack[match.end()] if match.end() < len(haystack) else " "
            if not (before.isalnum() and needle[0].isalnum()) and not (
                after.isalnum() and needle[-1].isalnum()
            ):
                return match.start()
    return -1


def quote_refs(source: SourceSnapshot, quote) -> list[Evidence]:
    return locate(source, quote)[0]


def quoted_text(source: SourceSnapshot, quote) -> str:
    return locate(source, quote)[1]


def document_node(source: SourceSnapshot, role: str, dataset: str) -> Node:
    return Node(
        id=f"document:{source.source_id}",
        kind="Document",
        properties={
            "source_id": source.source_id,
            "path": source.path,
            "sha256": source.sha256,
            "recorded_at": source.registered_at,
            "text": source.text,
            "dataset": dataset,
            "role": role,
            **(
                {"page_numbers_json": json.dumps(source.page_numbers)}
                if source.page_numbers
                else {}
            ),
        },
        evidence=evidence_at(source, [1]),
    )


# Agents normalise dates (they read '13-10-2026', 'Friday 11 Oct' or '14/10/2026');
# code checks that the normalised value is actually written in the quoted text.
def supported_date(value: str, text: str) -> date:
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        raise ValueError(f"Date {value!r} is not an ISO date.") from None
    lowered = text.casefold()
    numbers = {int(n) for n in re.findall(r"\d+", lowered)}
    month_name = parsed.strftime("%b").casefold()
    if not (
        (parsed.year in numbers or parsed.year % 100 in numbers)
        and parsed.day in numbers
        and (parsed.month in numbers or month_name in lowered)
    ):
        raise ValueError(f"Date {value} is not supported by the quoted text {text!r}.")
    return parsed


def supported_time(moment: datetime, text: str):
    pattern = rf"(?<!\d)0?{moment.hour}[:.]{moment.minute:02d}(?!\d)"
    if not re.search(pattern, text):
        raise ValueError(f"Time {moment:%H:%M} is not supported by the quoted text {text!r}.")


def plant_time(value: str, zone: ZoneInfo) -> datetime:
    """Local times without an offset use the plant timezone declared in the manifest."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Time {value!r} is not an ISO date-time.") from None
    if moment.utcoffset() is None:
        return moment.replace(tzinfo=zone)
    if moment.utcoffset() != moment.replace(tzinfo=zone).utcoffset():
        raise ValueError(f"Time {value} uses an offset other than the plant timezone.")
    return moment


def not_after_registration(moment: datetime | date, source: SourceSnapshot):
    registered = datetime.fromisoformat(source.registered_at)
    limit = registered if isinstance(moment, datetime) else registered.date()
    if moment > limit:
        raise ValueError("Recorded time is later than the file's registration.")
