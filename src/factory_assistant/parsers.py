"""Deterministic readers for structured formats: registers, controller logs, table rows.

Triage decides which file is which; these parsers only handle the layout. Rows that
do not fit are reported, never repaired by guessing.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from factory_assistant.evidence import evidence_at, not_after_registration, plant_time
from factory_assistant.facts import AssetFact, EventFact
from factory_assistant.models import SourceSnapshot

REGISTER_COLUMNS = ["id", "name", "aliases", "parent", "model", "configuration"]


def parse_equipment_csv(source: SourceSnapshot) -> list[AssetFact]:
    """An asset register export. Aliases are '|'-separated; blank means unknown."""
    reader = csv.DictReader(io.StringIO(source.text), strict=True)
    if reader.fieldnames is None or sorted(reader.fieldnames) != sorted(REGISTER_COLUMNS):
        raise ValueError("Register columns differ from " + ", ".join(REGISTER_COLUMNS) + ".")
    assets, previous_line = [], reader.line_num
    for raw in reader:
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError("Register row has missing or extra cells.")
        row = {key: value.strip() for key, value in raw.items()}
        refs = evidence_at(source, [1, *range(previous_line + 1, reader.line_num + 1)])
        previous_line = reader.line_num
        if not row["id"] or not row["name"]:
            raise ValueError("Register row needs an ID and a name.")
        if bool(row["model"]) != bool(row["configuration"]):
            raise ValueError("Record both model and configuration, or leave both empty.")
        assets.append(
            AssetFact(
                id=row["id"],
                name=row["name"],
                aliases=[a.strip() for a in row["aliases"].split("|") if a.strip()],
                model=row["model"] or None,
                configuration=row["configuration"] or None,
                parent=row["parent"] or None,
                evidence=refs,
                parent_evidence=refs if row["parent"] else [],
            )
        )
    if not assets:
        raise ValueError("Register has no rows.")
    return assets


# Controller exports are dd/mm (plant convention) or ISO; other layouts are held.
LOCAL_FORMATS = ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M")


def log_time(cell: str, zone: ZoneInfo) -> datetime:
    try:
        return plant_time(cell, zone)
    except ValueError:
        for layout in LOCAL_FORMATS:
            try:
                return datetime.strptime(cell, layout).replace(tzinfo=zone)
            except ValueError:
                continue
    raise ValueError(f"Unrecognised timestamp {cell!r}.")


def parse_event_log(source: SourceSnapshot, zone: ZoneInfo) -> tuple[list[EventFact], list[dict]]:
    """Rows: time | event=ID | equipment | CODE | message (';' also accepted).

    The same event exported twice becomes one event with both source lines. Two
    different rows sharing an ID are both held: we cannot tell which one is right.
    """
    rows, conflicts, held = {}, set(), []
    for number, line in enumerate(source.text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        event_id = None
        try:
            delimiter = "|" if "|" in line else ";"
            # Split four times only: the message may itself contain the delimiter.
            cells = [part.strip() for part in line.split(delimiter, 4)]
            if len(cells) < 4 or not re.fullmatch(r"event=[\w-]+", cells[1]):
                raise ValueError("Row does not match the controller export layout.")
            event_id = cells[1].removeprefix("event=")
            occurred = log_time(cells[0], zone)
            not_after_registration(occurred, source)
            values = {
                "event_id": event_id,
                "occurred_at": occurred.isoformat(),
                "mention": cells[2],
                "code": cells[3],
                "message": cells[4] if len(cells) > 4 else "",
            }
            refs = evidence_at(source, [number])
            if event_id in rows and rows[event_id][0] != values:
                conflicts.add(event_id)
                raise ValueError("Another row has the same event ID but different content.")
            if event_id in rows:
                rows[event_id][1].extend(refs)
            else:
                rows[event_id] = (values, refs)
        except ValueError as exc:
            held.append({"line": number, "item": event_id or "row", "reason": str(exc)})
    events = [
        EventFact(**values, evidence=refs)
        for event_id, (values, refs) in rows.items()
        if event_id not in conflicts
    ]
    return events, held


def split_row(line: str, delimiter: str) -> list[str]:
    if delimiter == "spaces":
        return [cell for cell in re.split(r"\s{2,}", line.strip()) if cell]
    actual = "\t" if delimiter == "tab" else delimiter
    return [cell.strip() for cell in next(csv.reader([line], delimiter=actual))]
