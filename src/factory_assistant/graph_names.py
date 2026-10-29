"""One-time vocabulary upgrade; source evidence and historical model output stay intact."""

from copy import deepcopy

LEGACY_RELATIONS = {
    "FEEDS": "SENDS_TO",
    "CONCERNS": "RECORDED_FOR",
    "HAS_ALARM": "REPORTS_ALARM",
    "COUNTS": "STOCK_OF",
}


def upgrade_saved_graph(payload: dict) -> dict:
    """Read earlier snapshots without rewriting their files or their source quotations."""
    result = deepcopy(payload)
    if result.get("format_version", 1) == 1:
        for node in result["nodes"]:
            if node["kind"] == "Asset":
                node["kind"] = "Equipment"
        for edge in result["edges"]:
            edge["relation"] = LEGACY_RELATIONS.get(edge["relation"], edge["relation"])
        result["format_version"] = 2
    if result.get("mode") == "offline":
        result["mode"] = "replay"  # Offline builds became replays of recorded decisions.
    return result


def upgrade_database_names(tx, project: str):
    """Rename this project's older graph copies, preserving all relationship evidence.

    This runs inside the graph publication transaction. It also updates older copies
    so Neo4j's label list does not show both the old and new names for our project.
    """
    tx.run(
        "MATCH (n:FactoryNode:Asset {project: $project}) SET n:Equipment REMOVE n:Asset",
        project=project,
    ).consume()
    for old, new in LEGACY_RELATIONS.items():
        # Both names come from the fixed mapping above, never from source/model text.
        tx.run(
            f"MATCH (a:FactoryNode {{project: $project}})-[old:{old}]->"
            "(b:FactoryNode {project: $project}) "
            f"MERGE (a)-[renamed:{new}]->(b) "
            "SET renamed += properties(old) DELETE old",
            project=project,
        ).consume()
