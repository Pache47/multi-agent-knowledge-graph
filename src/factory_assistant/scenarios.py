"""Choose which folder's knowledge a question uses, without mixing datasets.

The main shared folder is built and saved from the workbench. Other folders under
data/scenarios are built on demand by the same pipeline (replaying recorded agent
decisions), which shows the pipeline needs no code change for a different line.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from factory_assistant.evidence import MAIN_FOLDER, load_manifest
from factory_assistant.graph_models import GraphBundle
from factory_assistant.query_tools import timestamp


def metadata(manifest: dict, bundle: GraphBundle) -> dict:
    latest = max(
        [
            manifest["question_at"],
            *[
                n.properties["recorded_at"]
                for n in bundle.nodes
                if n.kind == "Document" and n.properties.get("dataset") == manifest["dataset"]
            ],
        ],
        key=timestamp,
    )
    return {
        "title": manifest["title"],
        "dataset": manifest["dataset"],
        "question_at": manifest["question_at"],
        "latest_question_at": latest,
        "question": manifest["question"],
        "examples": manifest.get("examples", []),
    }


def select_scenario(root: Path, runtime: Path, name: str, bundle=None) -> tuple[dict, GraphBundle]:
    if name != "main":
        return load_scenario(root, name)
    from factory_assistant.pipeline import latest_bundle

    bundle = bundle or latest_bundle(runtime)
    if bundle is None:
        raise ValueError("Build the main graph before asking about it.")
    return metadata(load_manifest(root, MAIN_FOLDER), bundle), bundle


def load_scenario(root: Path, name: str) -> tuple[dict, GraphBundle]:
    """Build a separate graph for data/scenarios/<name>; never touches the saved main KG."""
    from factory_assistant.pipeline import build_knowledge

    folder = f"data/scenarios/{name}"
    if not (root / folder).resolve().is_relative_to((root / "data/scenarios").resolve()):
        raise ValueError("Scenario must be a folder inside data/scenarios.")
    manifest = load_manifest(root, folder)
    with TemporaryDirectory() as runtime:
        bundle = build_knowledge(root, Path(runtime), "replay", folder)
    return metadata(manifest, bundle), bundle
