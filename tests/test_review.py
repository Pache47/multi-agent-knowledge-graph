"""Schema review: new stock columns wait for a person; approved counts stay dated records."""

from uuid import uuid4

import pytest
from conftest import FakeAgents, make_folder

from factory_assistant.models import FieldDefinition, ReviewDecision
from factory_assistant.pipeline import build_knowledge
from factory_assistant.query_tools import GraphTools
from factory_assistant.review import SchemaReview

LATE = "2099-01-01T00:00:00+00:00"
INCIDENT = "2026-10-14T08:20:00+08:00"


def decide(runtime, action="approve", fields=None):
    review = SchemaReview(runtime / "review.sqlite")
    proposal = next(p for p in review.proposals() if p["status"] in {"pending", "deferred"})
    return review.decide(
        proposal["id"],
        ReviewDecision(
            action=action,
            reviewer="Test reviewer",
            proposal_fingerprint=proposal["fingerprint"],
            edited_fields=fields,
            event_id=str(uuid4()),
        ),
    )


def stock(bundle, cutoff=LATE):
    tools = GraphTools(bundle, "initial-incident", cutoff, "CV-01")
    tools.find_equipment("CV-01")
    return tools, tools.query_stock("CV-01")["stock"]


def test_new_columns_wait_for_review_without_blocking_other_files(project):
    runtime = project / "runtime"
    bundle = build_knowledge(project, runtime, "replay")
    assert any(h["source"] == "C3" and h["stage"] == "schema review" for h in bundle.held)
    assert not any(r.source_id == "C3" for n in bundle.nodes for r in n.evidence)
    proposal = SchemaReview(runtime / "review.sqlite").proposals()[0]
    assert proposal["status"] == "pending" and "Column mapping" in proposal["evidence"]
    # The original stores snapshot needed no new fields, so it published directly.
    tools, summaries = stock(bundle, INCIDENT)
    assert [(s["part_id"], s["location"], s["quantities"]) for s in summaries] == [
        ("RA-200B", "B-12", {"quantity": 1})
    ]
    text = tools.facts[summaries[0]["record_summary_id"]].text
    assert "Reservations: not recorded" in text and "Free stock is not calculated" in text


def test_approval_publishes_on_the_next_replay_and_keeps_the_incident_view(project):
    runtime = project / "runtime"
    build_knowledge(project, runtime, "replay")
    decide(runtime, "approve")
    bundle = build_knowledge(project, runtime, "replay")
    assert not any(h["stage"] == "schema review" for h in bundle.held)
    records = [
        n for n in bundle.nodes if n.kind == "StockRecord" and n.evidence[0].source_id == "C3"
    ]
    assert len(records) == 2 and all(r.properties["schema_version"] == 2 for r in records)
    assert {r.properties["field_quantity_reserved"] for r in records} == {0, 1}
    _, later = stock(bundle)
    assert any("quantity_reserved" in s["quantities"] for s in later)
    # The later file also cites RA-200B; the incident view keeps the approval and the
    # count known at the time, with only the evidence available then.
    tools, incident = stock(bundle, INCIDENT)
    assert [(s["part_id"], s["quantities"]) for s in incident] == [("RA-200B", {"quantity": 1})]
    assert {r.source_id for r in tools.nodes["part:RA-200B"].evidence} == {"E7a", "E7b"}


@pytest.mark.parametrize("action", ["reject", "defer"])
def test_rejected_or_deferred_columns_keep_their_records_out(project, action):
    runtime = project / "runtime"
    build_knowledge(project, runtime, "replay")
    assert decide(runtime, action)["status"] == {"reject": "rejected", "defer": "deferred"}[action]
    bundle = build_knowledge(project, runtime, "replay")
    assert any(h["source"] == "C3" for h in bundle.held)
    assert not any(r.source_id == "C3" for n in bundle.nodes for r in n.evidence)


def test_edit_needs_another_approval_and_stale_forms_fail(project):
    runtime = project / "runtime"
    build_knowledge(project, runtime, "replay")
    review = SchemaReview(runtime / "review.sqlite")
    first = review.proposals()[0]
    edited = [
        FieldDefinition(
            name=f["name"], data_type=f["data_type"], description="Units reserved for open jobs"
        )
        for f in first["fields"]
    ]
    revised = decide(runtime, "edit", edited)
    assert revised["status"] == "pending" and revised["revision"] == 2
    stale = ReviewDecision(
        action="approve",
        reviewer="Test",
        proposal_fingerprint=first["fingerprint"],
        event_id=str(uuid4()),
    )
    with pytest.raises(ValueError, match="stale"):
        review.decide(first["id"], stale)
    with pytest.raises(ValueError, match="exactly the proposed columns"):
        other = FieldDefinition(name="other", data_type="string", description="Something else")
        decide(runtime, "edit", [other])
    decide(runtime, "approve")
    assert review.schema().by_name()[edited[0].name].description == "Units reserved for open jobs"


# Counts from scripted stock files -------------------------------------------------

REGISTER = "id,name,aliases,parent,model,configuration\nCV-01,Conveyor,outfeed belt,,CT-200,B\n"
PARTS = "Approved parts list\nCT-200 configuration B: part RA-200B\n"
HEADER = "part_id,quantity_on_hand,quantity_reserved,location,counted_at"


def table(field_on_hand="quantity_on_hand"):
    return {
        "header": {"line": 1, "text": HEADER},
        "delimiter": ",",
        "columns": [
            {
                "column": "part_id",
                "field": "part_id",
                "new_field_type": None,
                "new_field_meaning": None,
            },
            {
                "column": "quantity_on_hand",
                "field": field_on_hand,
                "new_field_type": "integer",
                "new_field_meaning": "Physical units counted in the bin",
            },
            {
                "column": "quantity_reserved",
                "field": "quantity_reserved",
                "new_field_type": "integer",
                "new_field_meaning": "Units of that count reserved for open jobs",
            },
            {
                "column": "location",
                "field": "location",
                "new_field_type": None,
                "new_field_meaning": None,
            },
            {
                "column": "counted_at",
                "field": "counted_at",
                "new_field_type": None,
                "new_field_meaning": None,
            },
        ],
        "counted_at": None,
        "counted_at_iso": None,
    }


def stock_build(tmp_path, rows: list[str], approve=True):
    files = {"assets.csv": REGISTER, "parts.txt": PARTS}
    files.update({f"stock{i}.csv": HEADER + "\n" + row + "\n" for i, row in enumerate(rows)})
    folder = make_folder(tmp_path, files)
    ids = [f"F{i}" for i in range(3, 3 + len(rows))]
    agents = FakeAgents(
        triage={"F1": "equipment_list", "F2": "approved_parts", **{i: "stock_list" for i in ids}},
        extract={
            ("F2", "approved_parts"): [
                {
                    "approvals": [
                        {
                            "part_id": {"line": 2, "text": "RA-200B"},
                            "description": None,
                            "model": {"line": 2, "text": "CT-200"},
                            "configuration": {"line": 2, "text": "B"},
                        }
                    ]
                }
            ],
            **{(i, "stock_list"): [table()] for i in ids},
        },
    )
    runtime = tmp_path / "runtime"
    bundle = build_knowledge(tmp_path, runtime, "replay", folder, agents=agents)
    if approve:
        decide(runtime, "approve")
        bundle = build_knowledge(tmp_path, runtime, "replay", folder, agents=agents)
    tools = GraphTools(bundle, "test", LATE, "CV-01")
    tools.find_equipment("CV-01")
    return bundle, tools, tools.query_stock("CV-01")["stock"]


def test_one_approval_covers_the_same_columns_in_later_files(tmp_path):
    bundle, _, summaries = stock_build(
        tmp_path,
        [
            "RA-200B,2,1,B-12,2026-10-15T16:00:00+08:00",
            "RA-200B,4,2,B-12,2026-10-16T16:00:00+08:00",
        ],
    )
    assert not bundle.held
    # The latest count per exact location is selected; older counts are not added.
    assert [s["quantities"] for s in summaries] == [{"quantity_on_hand": 4, "quantity_reserved": 2}]


@pytest.mark.parametrize(
    "second, expected", [("2,1", "recorded_counts"), ("3,1", "conflicting_counts")]
)
def test_same_time_counts_are_never_summed_or_chosen(tmp_path, second, expected):
    _, tools, summaries = stock_build(
        tmp_path,
        [
            "RA-200B,2,1,B-12,2026-10-15T16:00:00+08:00",
            f"RA-200B,{second},B-12,2026-10-15T16:00:00+08:00",
        ],
    )
    assert len(summaries) == 1 and summaries[0]["coverage"] == expected
    if expected == "conflicting_counts":
        assert summaries[0]["quantities"] == {}
        assert "No quantity selected" in tools.facts[summaries[0]["record_summary_id"]].text


def test_conflicting_rows_in_one_file_are_not_published_as_one_count(tmp_path):
    bundle, tools, summaries = stock_build(
        tmp_path,
        ["RA-200B,2,1,B-12,2026-10-15T16:00:00+08:00\nRA-200B,9,1,B-12,2026-10-15T16:00:00+08:00"],
    )
    assert len([n for n in bundle.nodes if n.kind == "StockRecord"]) == 2
    assert len(summaries) == 1 and summaries[0]["coverage"] == "conflicting_counts"
    assert summaries[0]["quantities"] == {} and len(summaries[0]["records"]) == 2


@pytest.mark.parametrize(
    "row, reason",
    [
        ("RA-200B,1,3,B-12,2026-10-15T16:00:00+08:00", "exceeds"),
        ("RA-200B,-1,0,B-12,2026-10-15T16:00:00+08:00", "non-negative"),
        ("RA-200B,1,0,B-12,2026-12-15T16:00:00+08:00", "later than"),
    ],
)
def test_approval_cannot_publish_invalid_counts(tmp_path, row, reason):
    bundle, _, _ = stock_build(tmp_path, [row])
    assert not [n for n in bundle.nodes if n.kind == "StockRecord"]
    assert any(reason in h["reason"] for h in bundle.held)


def test_a_stock_row_without_a_part_or_location_is_held_not_fatal(tmp_path):
    bundle, _, summaries = stock_build(
        tmp_path,
        [
            "RA-200B,2,1,B-12,2026-10-15T16:00:00+08:00",
            ",3,0,B-12,2026-10-15T16:00:00+08:00",
            "RA-200B,5,0,,2026-10-15T16:00:00+08:00",
        ],
    )
    assert [s["quantities"]["quantity_on_hand"] for s in summaries] == [2]
    reasons = {h["source"]: h["reason"] for h in bundle.held if h["stage"] == "stock"}
    assert reasons == {"F4": "The row has no part_id.", "F5": "The row has no location."}
