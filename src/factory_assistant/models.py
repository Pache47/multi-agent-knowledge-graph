"""Stable workflow records; the approved domain schema is versioned data, not Python code."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldDefinition(Record):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    data_type: Literal["string", "integer", "number", "boolean"]
    description: str = Field(min_length=8, max_length=600)


class StockSchema(Record):
    version: int
    fields: list[FieldDefinition]

    def by_name(self) -> dict[str, FieldDefinition]:
        return {field.name: field for field in self.fields}


def initial_schema() -> StockSchema:
    """Missing quantities stay missing: schema fields do not imply default values."""
    return StockSchema(
        version=1,
        fields=[
            FieldDefinition(
                name="part_id", data_type="string", description="Replacement part reference"
            ),
            FieldDefinition(
                name="description", data_type="string", description="Part description as recorded"
            ),
            FieldDefinition(
                name="quantity", data_type="integer", description="Recorded physical count"
            ),
            FieldDefinition(
                name="location", data_type="string", description="Stores bin or location"
            ),
            FieldDefinition(
                name="counted_at", data_type="string", description="Time of the stock count"
            ),
        ],
    )


class SourceSnapshot(Record):
    source_id: str
    path: str
    sha256: str
    text: str
    registered_at: str
    # Flattened PDF lines keep their physical page; plain-text sources leave this empty.
    page_numbers: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_pages(self):
        if self.page_numbers and (
            len(self.page_numbers) != len(self.text.splitlines())
            or any(page < 1 for page in self.page_numbers)
        ):
            raise ValueError("PDF page mapping must cover every extracted line.")
        return self


class Proposal(Record):
    base_version: int
    revision: int = 1
    fields: list[FieldDefinition]
    reason: str
    evidence_quote: str

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()


class ReviewDecision(Record):
    action: Literal["approve", "edit", "reject", "defer"]
    reviewer: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    proposal_fingerprint: str
    edited_fields: list[FieldDefinition] | None = None
    event_id: str
