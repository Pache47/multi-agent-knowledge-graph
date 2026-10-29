"""Files added in the app: built with the folder, kept out of data/, removable."""

import json
from types import SimpleNamespace

import pytest
from conftest import RECORDED, FakeAgents, make_folder
from test_pipeline import LOG, REGISTER

from factory_assistant.agents import ModelAgents
from factory_assistant.pipeline import build_knowledge
from factory_assistant.query_tools import GraphTools
from factory_assistant.uploads import add_file, added_files, remove_file


def test_an_added_file_is_built_with_the_folder_and_removed_cleanly(tmp_path):
    folder = make_folder(tmp_path, {"assets.csv": REGISTER})
    runtime = tmp_path / "runtime"
    entry = add_file(runtime, "export.log", LOG.encode(), RECORDED)
    assert entry["id"] == "U1" and not (tmp_path / folder / "export.log").exists()
    agents = FakeAgents(triage={"F1": "equipment_list", "U1": "event_log"})
    steps = []
    bundle = build_knowledge(
        tmp_path, runtime, "replay", folder, agents=agents, progress=lambda s, _: steps.append(s)
    )
    assert steps[0] == "register" and steps[-1] == "assemble_graph"
    event = next(n for n in bundle.nodes if n.kind == "Event")
    assert event.id == "U1:A-1" and event.evidence[0].path == "uploads/U1/export.log"
    tools = GraphTools(bundle, "test", "2099-01-01T00:00:00+00:00", "CV-01")
    tools.find_equipment("CV-01")
    assert tools.query_events("CV-01", None, None)["count"] == 1
    # Removing the file removes its facts on the next build; nothing else changes.
    remove_file(runtime, "U1")
    assert added_files(runtime) == []
    bundle = build_knowledge(tmp_path, runtime, "replay", folder, agents=agents)
    assert not [n for n in bundle.nodes if n.kind == "Event"]
    assert "CV-01" in {n.id for n in bundle.nodes}


@pytest.mark.parametrize("name", ["notes.exe", "../escape.txt/..", ""])
def test_only_plain_supported_files_can_be_added(tmp_path, name):
    with pytest.raises(ValueError):
        add_file(tmp_path, name, b"text", RECORDED)


def test_decisions_about_added_files_stay_in_the_runtime_folder(tmp_path):
    folder = make_folder(tmp_path, {"lunch.txt": "Canteen menu\n"})
    runtime = tmp_path / "runtime"
    add_file(runtime, "menu.txt", b"Canteen menu for Friday\n", RECORDED)
    repo_cache = tmp_path / "agent_cache"
    agents = ModelAgents(repo_cache, "live", added_cache=runtime / "uploads" / "decisions")
    decision = {"doc_type": "unsupported", "alternative": None, "reason": "a menu"}

    class Parsed:
        model = "fake"
        usage = None

        def __init__(self, contract):
            self.output_parsed = contract.model_validate(decision)

    agents._client = SimpleNamespace(
        responses=SimpleNamespace(parse=lambda **kw: Parsed(kw["text_format"]))
    )
    build_knowledge(tmp_path, runtime, "live", folder, agents=agents)
    recorded = {p.parent.name: json.loads(p.read_text())["for"] for p in tmp_path.rglob("triage-*")}
    assert recorded == {"agent_cache": "F1", "decisions": "U1"}
