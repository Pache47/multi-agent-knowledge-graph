"""Construction pipeline: routing, source checks, corrections, identity and replay."""

from pathlib import Path

import pytest
from conftest import ROOT, FakeAgents, make_folder

from factory_assistant.agents import ModelAgents
from factory_assistant.contracts import Quote
from factory_assistant.evidence import locate
from factory_assistant.models import SourceSnapshot
from factory_assistant.parsers import parse_event_log
from factory_assistant.pipeline import build_knowledge, gold_check
from factory_assistant.scenarios import load_scenario

GOLD = ("construction_gold.json", "document_gold.json", "maintenance_gold.json")

REGISTER = (
    "id,name,aliases,parent,model,configuration\n"
    "CV-01,Outfeed conveyor,outfeed belt|packing outfeed,,CT-200,B\n"
    "CV-02,Case packer outfeed,case packer outfeed,,CT-200,A\n"
    "P-22,Rinse return pump,rinse pump,,,\n"
)


def q(line, text):
    return {"line": line, "text": text}


def case(line, reference, date, iso, equipment, problem):
    return {
        "reference": q(line, reference) if reference else None,
        "date": q(line, date),
        "occurred_on": iso,
        "equipment": q(line, equipment),
        "problem": q(line + 1, problem),
        "finding": None,
        "finding_status": None,
        "work": None,
        "outcome": None,
        "configuration": None,
    }


def build(root, folder, agents, tmp_path):
    return build_knowledge(root, tmp_path / "runtime", "replay", folder, agents=agents)


# Replay of the recorded build ------------------------------------------------------


def test_replay_reproduces_the_recorded_graph_without_model_calls(tmp_path):
    agents = ModelAgents(ROOT / "agent_cache", "replay")
    bundle = build_knowledge(ROOT, tmp_path, "replay", agents=agents)
    reports = [gold_check(bundle, ROOT / "evaluation" / name) for name in GOLD]
    failed = [c["check"] for r in reports for c in r["checks"] if not c["passed"]]
    assert failed == [] and sum(r["total"] for r in reports) == 53
    assert agents._client is None  # No client was ever created.
    # Held: an ambiguous callout, new stock columns, and any link to unidentified equipment.
    held = {(h["source"], h["stage"]) for h in bundle.held}
    assert {("C3", "schema review"), ("N3", "identity")} <= held
    assert all(
        h["source"] in {"C3", "N3"} or h["reason"] == "Flow names unknown equipment."
        for h in bundle.held
    )
    routes = {t["source_id"]: t["method"] for t in bundle.extraction_trace}
    assert routes["E2"] == "parser: controller log"
    assert routes["R1"] == "parser: equipment register"
    assert routes["E5"] == routes["N4"] == "agent: extraction"
    # Duplicate exports count once; every fact keeps exact source lines.
    overloads = [
        n for n in bundle.nodes if n.kind == "Event" and n.properties["code"] == "MOTOR_OVERLOAD"
    ]
    assert len(overloads) == 2
    lines = {
        n.properties["source_id"]: n.properties["text"].splitlines()
        for n in bundle.nodes
        if n.kind == "Document"
    }
    assert all(
        r.excerpt == lines[r.source_id][r.line - 1]
        for item in [*bundle.nodes, *bundle.edges]
        for r in item.evidence
    )


def test_missing_decisions_hold_files_instead_of_guessing(tmp_path):
    agents = ModelAgents(tmp_path / "empty-cache", "replay")
    bundle = build_knowledge(ROOT, tmp_path, "replay", agents=agents)
    assert not [n for n in bundle.nodes if n.kind != "Document"]
    assert {h["stage"] for h in bundle.held} == {"triage"}
    assert all("run a live build" in h["reason"] for h in bundle.held)


def test_changed_file_needs_new_decisions_while_others_replay(project):
    path = project / "data/shared_folder/shift_handover.txt"
    path.write_text(path.read_text().replace("scraping", "squealing"))
    bundle = build_knowledge(project, project / "runtime", "replay")
    assert [h["source"] for h in bundle.held if "run a live build" in h["reason"]] == ["E3"]
    report = gold_check(bundle, project / "evaluation/construction_gold.json")
    assert report["passed"] == report["total"]  # Equipment and logs are unaffected.


def test_same_pipeline_builds_another_line_with_no_code_change():
    metadata, bundle = load_scenario(ROOT, "sensor_stop")
    assert metadata["dataset"] == "sensor-stop-demo" and not bundle.held
    edges = {(e.source, e.relation, e.target) for e in bundle.edges}
    assert ("document:S3", "APPLIES_TO", "CB-50:C") in edges
    assert ("document:S5", "DESCRIBES", "procedure:SITE-M02") in edges
    events = [
        n for n in bundle.nodes if n.kind == "Event" and n.properties["code"] == "SENSOR_BLOCKED"
    ]
    assert len(events) == 3 and all(e.properties["asset_id"] == "PS-10" for e in events)


# Quotes and values ----------------------------------------------------------------


def snapshot(text):
    return SourceSnapshot(
        source_id="X", path="data/x.txt", sha256="x", text=text, registered_at=RECORDED_AT
    )


RECORDED_AT = "2026-10-18T08:00:00+08:00"


def test_quotes_match_wording_not_paraphrases():
    source = snapshot('CV-01 = "outfeed belt". Model CT-200, configuration B.\nnext line here')
    refs, text = locate(source, Quote(line=1, text="B"))
    assert text == "B" and refs[0].line == 1  # Not the 'b' in 'belt'.
    assert locate(source, Quote(line=1, text="configuration b."))[1] == "configuration B."
    refs, _ = locate(source, Quote(line=1, text="configuration B. next line"))
    assert [r.line for r in refs] == [1, 2]
    for bad in (Quote(line=1, text="belt is outfeed"), Quote(line=2, text="CV-01")):
        with pytest.raises(ValueError):
            locate(source, bad)


def walkdown_answer(alias="outfeed belt", part_of=True):
    return {
        "assets": [
            {
                "id": q(2, "CV-01"),
                "name": None,
                "aliases": [q(2, alias)],
                "model": q(2, "CT-200"),
                "configuration": q(2, "B"),
                "part_of": None,
                "part_of_lines": [],
            },
            {
                "id": q(3, "D-01"),
                "name": q(3, "Drive"),
                "aliases": [],
                "model": None,
                "configuration": None,
                "part_of": "CV-01" if part_of else None,
                "part_of_lines": [3] if part_of else [],
            },
        ],
        "flows": [],
    }


WALKDOWN = (
    'Walkdown 1 Oct 2026\nCV-01 = "outfeed belt". Model CT-200, configuration B.\nDrive D-01.\n'
)


def test_failed_quote_gets_one_correction_with_code_feedback(tmp_path):
    folder = make_folder(tmp_path, {"walkdown.txt": WALKDOWN})
    agents = FakeAgents(
        triage={"F1": "equipment_list"},
        extract={("F1", "equipment_list"): [walkdown_answer("conveyor belt"), walkdown_answer()]},
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    feedback = [c[3] for c in agents.calls if c[0] == "extract" and c[3]]
    assert len(feedback) == 1 and "'conveyor belt'" in feedback[0][0]
    cv01 = next(n for n in bundle.nodes if n.id == "CV-01")
    assert cv01.properties["aliases"] == ["outfeed belt"] and not bundle.held
    component = next(e for e in bundle.edges if e.relation == "HAS_COMPONENT")
    assert {r.line for r in component.evidence} == {2, 3}  # Child listing and parent line.


def test_correction_that_loses_facts_is_not_accepted(tmp_path):
    folder = make_folder(tmp_path, {"walkdown.txt": WALKDOWN})
    agents = FakeAgents(
        triage={"F1": "equipment_list"},
        extract={
            ("F1", "equipment_list"): [
                walkdown_answer("conveyor belt"),
                {"assets": [], "flows": []},
            ]
        },
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    assert {"D-01"} <= {n.id for n in bundle.nodes}
    assert any("conveyor belt" in h["reason"] for h in bundle.held)


def test_a_sentence_is_not_accepted_as_a_label(tmp_path):
    text = "SIMULATED GUIDE CB-50 / configuration C\nApplies to CB-50 configuration C.\n"
    folder = make_folder(tmp_path, {"guide.txt": text})
    sentence = {
        "document_id": q(1, "SIMULATED GUIDE CB-50 / configuration C"),
        "revision": None,
        "issued": None,
        "issued_on": None,
        "archived": False,
        "applies_to": [{"model": q(2, "CB-50"), "configuration": q(2, "C")}],
        "section_headings": [],
        "alarm_definitions": [],
        "procedure_references": [],
    }
    agents = FakeAgents(triage={"F1": "manual"}, extract={("F1", "manual"): [sentence]})
    bundle = build(tmp_path, folder, agents, tmp_path)
    feedback = next(c[3] for c in agents.calls if c[0] == "extract" and c[3])
    assert "candidates in that text: CB-50" in feedback[0] and "or null" in feedback[0]
    # The rest of the manual still publishes; only the bad value is held.
    assert any(e.relation == "APPLIES_TO" and e.target == "CB-50:C" for e in bundle.edges)


# Routing --------------------------------------------------------------------------


LOG = (
    "# export; plant time\n"
    "2026-10-14T07:58:00+08:00 | event=A-1 | CV-01 | MOTOR_OVERLOAD | stopped\n"
    "14/10/2026 07:58:00 ; event=A-1 ; CV-01 ; MOTOR_OVERLOAD ; stopped\n"
    "2026-10-14T08:10:00+08:00 | event=A-2 | CV-01 | RUNNING | restart\n"
    "2026-10-14T08:12:00+08:00 | event=A-2 | CV-01 | RUNNING | different\n"
    "garbage row\n"
)


def test_logs_and_registers_use_parsers_and_other_layouts_fall_back(tmp_path):
    folder = make_folder(
        tmp_path,
        {
            "export.log": LOG,
            "assets.csv": REGISTER,
            "other.csv": "tag;description\nPE-09;spare sensor\n",
            "lunch.txt": "Canteen menu for Friday\n",
        },
    )
    agents = FakeAgents(
        triage={"F1": "event_log", "F2": "equipment_list", "F3": "equipment_list"},
        extract={("F3", "equipment_list"): [{"assets": [], "flows": []}]},
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    routes = {t["source_id"]: t["method"] for t in bundle.extraction_trace}
    assert routes == {
        "F1": "parser: controller log",
        "F2": "parser: equipment register",
        "F3": "agent: extraction",  # The register parser declined; the agent read it.
        "F4": "held",
    }
    events = {n.id: n for n in bundle.nodes if n.kind == "Event"}
    assert set(events) == {"F1:A-1"} and len(events["F1:A-1"].evidence) == 2
    reasons = " ".join(h["reason"] for h in bundle.held)
    assert "same event ID but different content" in reasons and "Unsupported" in reasons
    assert "document:F4" not in {n.id for n in bundle.nodes}


def test_parser_reads_day_first_local_times_in_plant_timezone():
    from zoneinfo import ZoneInfo

    events, held = parse_event_log(snapshot(LOG), ZoneInfo("Asia/Singapore"))
    assert events[0].occurred_at == "2026-10-14T07:58:00+08:00"
    assert [h["item"] for h in held] == ["A-2", "row"]  # A conflict and an unreadable row.
    assert [e.event_id for e in events] == ["A-1"]  # Neither conflicting copy is chosen.


def test_reroute_when_the_first_reading_yields_nothing(tmp_path):
    text = "Closeout WO-9\n11 Oct 2026, CV01.\nBelt creeping left.\n"
    folder = make_folder(tmp_path, {"assets.csv": REGISTER, "closeout.txt": text})
    agents = FakeAgents(
        triage={"F1": "equipment_list", "F2": "handover"},
        alternative={"F2": "maintenance_history"},
        extract={
            ("F2", "handover"): [{"date": q(2, "11 Oct 2026"), "scope": None, "entries": []}],
            ("F2", "maintenance_history"): [
                {
                    "cases": [
                        case(2, None, "11 Oct 2026", "2026-10-11", "CV01", "Belt creeping left.")
                    ]
                }
            ],
        },
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    trace = next(t for t in bundle.extraction_trace if t["source_id"] == "F2")
    assert trace["doc_type"] == "maintenance_history"
    assert any(s["stage"] == "reroute" and s["kept"] for s in trace["steps"])
    assert any(
        n.kind == "MaintenanceCase" and n.properties["asset_id"] == "CV-01" for n in bundle.nodes
    )


# Identity -------------------------------------------------------------------------


def test_code_resolves_exact_names_and_the_agent_only_gets_the_rest(tmp_path):
    text = (
        "Jobs\n"
        "WO-1 / 1 Oct 2026 / CV01\nBelt slipping.\n"
        "WO-2 / 2 Oct 2026 / packing outfeed\nNoise.\n"
        "WO-3 / 3 Oct 2026 / the outfeed\nStopping.\n"
        "WO-4 / 4 Oct 2026 / pump near rinse tank\nDrip.\n"
        "WO-5 / 5 Oct 2026 / mixer\nNoise.\n"
    )
    cases = [
        case(2 * i, f"WO-{i}", f"{i} Oct 2026", f"2026-10-0{i}", mention, problem)
        for i, (mention, problem) in enumerate(
            [
                ("CV01", "Belt slipping."),
                ("packing outfeed", "Noise."),
                ("the outfeed", "Stopping."),
                ("pump near rinse tank", "Drip."),
                ("mixer", "Noise."),
            ],
            1,
        )
    ]
    folder = make_folder(tmp_path, {"assets.csv": REGISTER, "jobs.txt": text})
    agents = FakeAgents(
        triage={"F1": "equipment_list", "F2": "maintenance_history"},
        extract={("F2", "maintenance_history"): [{"cases": cases}]},
        reconcile={
            "the outfeed": ("ambiguous", None),
            "pump near rinse tank": ("match", "P-22"),
            "mixer": ("match", "MX-1"),  # Not in the registry: never accepted.
        },
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    asked = next(c[2] for c in agents.calls if c[0] == "reconcile")
    assert asked == ["the outfeed", "pump near rinse tank", "mixer"]
    published = {
        n.properties["case_id"]: n.properties for n in bundle.nodes if n.kind == "MaintenanceCase"
    }
    assert {k: v["asset_id"] for k, v in published.items()} == {
        "WO-1": "CV-01",
        "WO-2": "CV-01",
        "WO-4": "P-22",
    }
    assert published["WO-4"]["identity_basis"].startswith("agent:")
    held = {h.get("item") for h in bundle.held if h["stage"] == "identity"}
    assert held == {"WO-3", "WO-5"}


def test_dates_must_be_written_in_the_quote_and_not_after_registration(tmp_path):
    text = "Jobs\nWO-1 / 1 Oct 2026 / CV-01\nNoise.\nWO-2 / 1 Dec 2026 / CV-01\nNoise.\n"
    folder = make_folder(tmp_path, {"assets.csv": REGISTER, "jobs.txt": text})
    agents = FakeAgents(
        triage={"F1": "equipment_list", "F2": "maintenance_history"},
        extract={
            ("F2", "maintenance_history"): [
                {
                    "cases": [
                        case(2, "WO-1", "1 Oct 2026", "2026-10-02", "CV-01", "Noise."),
                        case(4, "WO-2", "1 Dec 2026", "2026-12-01", "CV-01", "Noise."),
                    ]
                }
            ]
        },
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    assert not [n for n in bundle.nodes if n.kind == "MaintenanceCase"]
    reasons = " ".join(h["reason"] for h in bundle.held)
    assert "2026-10-02 is not supported" in reasons and "later than the file's" in reasons


def test_old_graph_names_upgrade_without_changing_evidence(tmp_path):
    from copy import deepcopy

    from factory_assistant.graph_models import GraphBundle
    from factory_assistant.graph_names import LEGACY_RELATIONS, upgrade_saved_graph

    bundle = build_knowledge(ROOT, tmp_path, "replay")
    legacy = bundle.model_dump()
    legacy.pop("format_version")
    legacy["mode"] = "offline"
    reverse = {value: key for key, value in LEGACY_RELATIONS.items()}
    for node in legacy["nodes"]:
        if node["kind"] == "Equipment":
            node["kind"] = "Asset"
    for edge in legacy["edges"]:
        edge["relation"] = reverse.get(edge["relation"], edge["relation"])
    original = deepcopy(legacy)
    upgraded = GraphBundle.model_validate(upgrade_saved_graph(legacy))
    assert legacy == original and upgraded == bundle


def test_root_paths_cannot_escape_data(tmp_path):
    from factory_assistant.evidence import load_manifest

    with pytest.raises(ValueError, match="inside data"):
        load_manifest(ROOT, "evaluation")
    assert Path(load_manifest(ROOT)["sources"][0]["path"]).parts[:2] == ("data", "shared_folder")


def test_one_event_in_two_exports_is_one_event_citing_both(tmp_path):
    later = (
        "2026-10-14T07:58:00+08:00 | event=A-1 | CV-01 | MOTOR_OVERLOAD | stopped\n"
        "2026-10-14T08:30:00+08:00 | event=A-3 | CV-01 | RUNNING | restart\n"
    )
    earlier = "2026-10-14T08:30:00+08:00 | event=A-3 | CV-01 | RUNNING | other text\n"
    folder = make_folder(
        tmp_path,
        {"assets.csv": REGISTER, "a.log": LOG, "b.log": later, "c.log": earlier},
    )
    agents = FakeAgents(
        triage={"F1": "equipment_list", "F2": "event_log", "F3": "event_log", "F4": "event_log"}
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    events = {n.id: n for n in bundle.nodes if n.kind == "Event"}
    assert set(events) == {"F2:A-1"}  # A-3 disagrees between files, so neither copy is chosen.
    assert {(r.source_id, r.line) for r in events["F2:A-1"].evidence} == {
        ("F2", 2),
        ("F2", 3),
        ("F3", 1),
    }
    held = {(h["source"], h.get("item")) for h in bundle.held if "differently" in h["reason"]}
    assert held == {("F3", "A-3"), ("F4", "A-3")}


class FailingAgents(FakeAgents):
    """Scripted agents whose correction or reconciliation call fails."""

    def __init__(self, *args, fail=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fail = set(fail)

    def extract(self, doc_type, source, previous=None, feedback=None):
        if feedback and "correct" in self.fail:
            raise ValueError("model unavailable")
        return super().extract(doc_type, source, previous, feedback)

    def reconcile(self, source, mentions, registry):
        if "reconcile" in self.fail:
            raise ValueError("model unavailable")
        return super().reconcile(source, mentions, registry)


def test_failed_correction_keeps_the_facts_that_passed(tmp_path):
    folder = make_folder(tmp_path, {"walkdown.txt": WALKDOWN})
    agents = FailingAgents(
        triage={"F1": "equipment_list"},
        extract={("F1", "equipment_list"): [walkdown_answer("conveyor belt")]},
        fail={"correct"},
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    # The drive passed its checks and stands; only the asset with the bad alias is held.
    assert "D-01" in {n.id for n in bundle.nodes} and "CV-01" not in {n.id for n in bundle.nodes}
    reasons = " ".join(h["reason"] for h in bundle.held)
    assert "conveyor belt" in reasons and "Correction not available" in reasons


def test_failed_reconciliation_keeps_exact_matches(tmp_path):
    text = (
        "Jobs\nWO-1 / 1 Oct 2026 / CV01\nBelt slipping.\nWO-2 / 2 Oct 2026 / the outfeed\nStop.\n"
    )
    cases = [
        case(2, "WO-1", "1 Oct 2026", "2026-10-01", "CV01", "Belt slipping."),
        case(4, "WO-2", "2 Oct 2026", "2026-10-02", "the outfeed", "Stop."),
    ]
    folder = make_folder(tmp_path, {"assets.csv": REGISTER, "jobs.txt": text})
    agents = FailingAgents(
        triage={"F1": "equipment_list", "F2": "maintenance_history"},
        extract={("F2", "maintenance_history"): [{"cases": cases}]},
        fail={"reconcile"},
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    published = {n.properties["case_id"] for n in bundle.nodes if n.kind == "MaintenanceCase"}
    assert published == {"WO-1"}
    held = [h for h in bundle.held if h["stage"] == "identity"]
    assert [h["item"] for h in held] == ["WO-2"] and "unavailable" in held[0]["reason"]


PARTS_LIST = (
    "Approved parts, CT-200\n"
    "Configuration B: part RA-200B, roller assembly.\n"
    "RA-200A and RA-200B are NOT approved substitutes for each other.\n"
    "Configuration A: part RA-300A is not approved.\n"
)


def approval(part_line, part, config_line, configuration):
    return {
        "part_id": q(part_line, part),
        "description": None,
        "model": q(1, "CT-200"),
        "configuration": q(config_line, configuration),
    }


def test_quotes_alone_do_not_make_an_approval(tmp_path):
    folder = make_folder(tmp_path, {"parts.txt": PARTS_LIST})
    approvals = [
        approval(2, "RA-200B", 2, "B"),  # A real approval.
        approval(3, "RA-200A", 2, "B"),  # Each value is in the file, but not as one statement.
        approval(4, "RA-300A", 4, "A"),  # One statement, but a prohibition.
    ]
    agents = FakeAgents(
        triage={"F1": "approved_parts"},
        extract={("F1", "approved_parts"): [{"approvals": approvals}]},
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    approved = {(e.source, e.target) for e in bundle.edges if e.relation == "APPROVED_FOR"}
    assert approved == {("part:RA-200B", "CT-200:B")}
    reasons = " ".join(h["reason"] for h in bundle.held)
    assert "same statement" in reasons and "restriction or exclusion" in reasons


def test_an_exclusion_is_not_applicability(tmp_path):
    text = "Guide CB-50\nApplies to CB-50 configuration C.\nNot for CB-50 configuration D.\n"
    folder = make_folder(tmp_path, {"guide.txt": text})
    manual = {
        "document_id": None,
        "revision": None,
        "issued": None,
        "issued_on": None,
        "archived": False,
        "applies_to": [
            {"model": q(2, "CB-50"), "configuration": q(2, "C")},
            {"model": q(3, "CB-50"), "configuration": q(3, "D")},
        ],
        "section_headings": [],
        "alarm_definitions": [],
        "procedure_references": [],
    }
    agents = FakeAgents(triage={"F1": "manual"}, extract={("F1", "manual"): [manual]})
    bundle = build(tmp_path, folder, agents, tmp_path)
    assert {e.target for e in bundle.edges if e.relation == "APPLIES_TO"} == {"CB-50:C"}


def test_a_record_cannot_borrow_fields_from_the_next_job(tmp_path):
    history = (
        "Job J-1 / 01 Oct 2026 / CV-01\n"
        "Reported: overload.\n"
        "Job J-2 / 02 Oct 2026 / CV-02\n"
        "Work: motor replaced.\n"
    )
    folder = make_folder(tmp_path, {"assets.csv": REGISTER, "history.txt": history})
    borrowed = case(1, "J-1", "01 Oct 2026", "2026-10-01", "CV-01", "overload")
    borrowed["work"] = q(4, "motor replaced")  # J-2's work, quoted correctly.
    own = case(3, "J-2", "02 Oct 2026", "2026-10-02", "CV-02", "motor replaced")
    agents = FakeAgents(
        triage={"F1": "equipment_list", "F2": "maintenance_history"},
        extract={("F2", "maintenance_history"): [{"cases": [borrowed, own]}] * 2},
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    cases = {n.id for n in bundle.nodes if n.kind == "MaintenanceCase"}
    assert cases == {"case:J-2"}
    assert any("another job's section" in h["reason"] for h in bundle.held)


def test_an_alarm_code_with_two_meanings_gets_neither(tmp_path):
    def manual(meaning_line, meaning):
        return {
            "document_id": None,
            "revision": None,
            "issued": None,
            "issued_on": None,
            "archived": False,
            "applies_to": [{"model": q(1, "CT-200"), "configuration": q(1, "B")}],
            "section_headings": [],
            "alarm_definitions": [{"code": q(meaning_line, "E01"), "meaning": q(2, meaning)}],
            "procedure_references": [],
        }

    log = "2026-10-14T07:58:00+08:00 | event=A-1 | CV-01 | E01 | stopped\n"
    folder = make_folder(
        tmp_path,
        {
            "assets.csv": REGISTER,
            "a.txt": "Guide CT-200 configuration B\nE01 means motor overload.\n",
            "b.txt": "Guide CT-200 configuration B\nE01 means low coolant.\n",
            "c.log": log,
        },
    )
    agents = FakeAgents(
        triage={"F1": "equipment_list", "F2": "manual", "F3": "manual", "F4": "event_log"},
        extract={
            ("F2", "manual"): [manual(2, "E01 means motor overload.")],
            ("F3", "manual"): [manual(2, "E01 means low coolant.")],
        },
    )
    bundle = build(tmp_path, folder, agents, tmp_path)
    assert not any(n.kind == "AlarmType" for n in bundle.nodes)
    assert not any(e.relation == "REPORTS_ALARM" for e in bundle.edges)
    held = {(h["source"], h["item"]) for h in bundle.held if "alarm code" in h["reason"]}
    assert held == {("F2", "E01"), ("F3", "E01")}
