"""Exercise the real workbench: replay build, schema review in a fresh session, evidence views."""

import shutil
from pathlib import Path

from streamlit.testing.v1 import AppTest

from factory_assistant.pipeline import build_knowledge, latest_bundle, save_bundle

ROOT = Path(__file__).resolve().parents[1]


def open_app(view: str | None = None) -> AppTest:
    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
    assert not app.exception
    if view:
        radio = next(r for r in app.radio if r.label == "Workbench")
        radio.set_value(next(o for o in radio.options if o.startswith(view))).run(timeout=30)
        assert not app.exception
    return app


def test_build_then_review_in_a_new_session_publishes_the_stock_file(tmp_path, monkeypatch):
    runtime = tmp_path / "ui-runtime"
    monkeypatch.setenv("FACTORY_RUNTIME", str(runtime))
    app = open_app("Build knowledge")
    next(b for b in app.button if b.label == "Build graph").click().run(timeout=60)
    assert not app.exception
    assert any("Knowledge saved" in s.value for s in app.success)
    assert not any(r.evidence[0].source_id == "C3" for r in latest_bundle(runtime).nodes)

    # A fresh session has no state: the proposal comes from SQLite, and the tab shows it.
    review = open_app("Review schema changes")
    workbench = next(r for r in review.radio if r.label == "Workbench")
    assert "Review schema changes (1)" in workbench.options
    next(t for t in review.text_input if t.label == "Reviewer name").set_value("UI reviewer")
    next(s for s in review.selectbox if s.label == "Decision").select("approve")
    next(b for b in review.button if b.label == "Save decision").click().run(timeout=60)
    assert not review.exception
    bundle = latest_bundle(runtime)
    assert any(n.kind == "StockRecord" and n.evidence[0].source_id == "C3" for n in bundle.nodes)


def equipment_page(tmp_path, monkeypatch, machine="CV-01"):
    runtime = tmp_path / "equipment-ui"
    save_bundle(runtime, build_knowledge(ROOT, runtime))
    monkeypatch.setenv("FACTORY_RUNTIME", str(runtime))
    app = open_app()
    next(s for s in app.selectbox if s.label == "Equipment").select(machine).run(timeout=30)
    assert not app.exception
    return app


def test_equipment_page_keeps_each_machines_history_separate(tmp_path, monkeypatch):
    labels = [e.label for e in equipment_page(tmp_path, monkeypatch).expander]
    assert any("J-071" in label for label in labels)
    assert not any("J-093" in label for label in labels)
    labels = [e.label for e in equipment_page(tmp_path, monkeypatch, "CV-02").expander]
    assert any("J-093" in label for label in labels)
    assert not any("J-071" in label for label in labels)


def test_equipment_page_follows_the_installed_configuration(tmp_path, monkeypatch):
    app = equipment_page(tmp_path, monkeypatch)
    text = " ".join(m.value for m in app.markdown)
    assert "CT200-B" in text and "CT200-A" not in text
    assert "RA-200B" in text and "RA-200A" not in text.split("RA-200B")[0]
    assert any(m.value == "CT-200 · B" for m in app.metric)
    shutil.rmtree(tmp_path / "equipment-ui")


def test_a_cited_file_opens_on_its_own_page(tmp_path, monkeypatch):
    runtime = tmp_path / "source-ui"
    bundle = build_knowledge(ROOT, runtime)
    save_bundle(runtime, bundle)
    monkeypatch.setenv("FACTORY_RUNTIME", str(runtime))
    document = next(n for n in bundle.nodes if n.kind == "Document" and n.id.endswith(":E1"))
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.query_params.update({"view": "E1", "set": "main", "lines": "2,999"})
    app.run(timeout=30)
    assert not app.exception
    assert Path(document.properties["path"]).name in app.title[0].value
    cited = document.properties["text"].splitlines()[1]
    assert app.code[0].value == f"line   2  {cited}"
    assert any(b.label == "Download the original file" for b in app.get("download_button"))

    app = AppTest.from_file(str(ROOT / "app.py"))
    app.query_params.update({"view": "../secrets", "set": "main"})
    app.run(timeout=30)
    assert "not in the current knowledge build" in app.error[0].value
