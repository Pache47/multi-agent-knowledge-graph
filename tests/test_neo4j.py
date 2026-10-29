"""Opt-in checks against a real local Neo4j. Each test owns and removes its graph scope."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from factory_assistant.graph_store import Neo4jProjection
from factory_assistant.pipeline import build_knowledge

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_NEO4J_TESTS") != "1",
    reason="Set RUN_NEO4J_TESTS=1 with local Neo4j running",
)


@pytest.fixture
def projection():
    with Neo4jProjection(ROOT, project=f"test-{uuid4()}") as graph:
        try:
            yield graph
        finally:
            with graph.driver.session(database="neo4j") as session:
                session.run(
                    "MATCH (n:FactoryNode {project: $project}) DETACH DELETE n",
                    project=graph.project,
                ).consume()
                session.run(
                    "MATCH (p:FactoryProject {name: $project}) DELETE p", project=graph.project
                ).consume()


def test_real_projection_idempotence_and_evidence(projection, tmp_path):
    bundle = build_knowledge(ROOT, tmp_path)
    first = projection.publish(bundle)
    assert projection.publish(bundle) == first
    assert projection.summary() == first
    with projection.driver.session(database="neo4j") as session:
        row = session.run(
            "MATCH (e:Event {project: $project, id: 'E2:DRV-441'})-[:RECORDED_FOR]->"
            "(drive:Equipment)<-[:HAS_COMPONENT]-(conveyor:Equipment {id: 'CV-01'}) "
            "RETURN e.evidence_json AS evidence, drive.id AS drive",
            project=projection.project,
        ).single()
        assert row["drive"] == "D-01"
        assert len(json.loads(row["evidence"])) == 2
        # The same projection must retain case-specific certainty and part approvals.
        history = session.run(
            "MATCH (c:MaintenanceCase {project: $project, id: 'case:J-071'})"
            "-[:HAS_FINDING]->(f:Finding) RETURN f.status AS status, c.action AS action",
            project=projection.project,
        ).single()
        assert history["status"] == "reported"
        assert "material removed" in history["action"].lower()
        approval = session.run(
            "MATCH (p:Part {project: $project, id: 'part:RA-200B'})"
            "-[:APPROVED_FOR]->(c:Configuration) RETURN c.id AS configuration",
            project=projection.project,
        ).single()
        assert approval["configuration"] == "CT-200:B"


def test_failed_projection_preserves_active_graph(projection, tmp_path, monkeypatch):
    bundle = build_knowledge(ROOT, tmp_path)
    before = projection.publish(bundle)
    changed = bundle.model_copy(deep=True)
    changed.nodes[0].properties["test_change"] = "forces another build"
    real_write = projection._write

    def fail_after_writes(tx, candidate):
        real_write(tx, candidate)
        raise ValueError("Simulated transaction failure")

    monkeypatch.setattr(projection, "_write", fail_after_writes)
    with pytest.raises(ValueError, match="Simulated"):
        projection.publish(changed)
    assert projection.summary() == before
    with projection.driver.session(database="neo4j") as session:
        row = session.run(
            "MATCH (n:FactoryNode {project: $project, build: $build}) RETURN count(n) AS count",
            project=projection.project,
            build=changed.fingerprint,
        ).single()
        assert row["count"] == 0


def test_old_database_names_are_upgraded_with_evidence_preserved(projection, tmp_path):
    bundle = build_knowledge(ROOT, tmp_path)
    expected = projection.publish(bundle)
    with projection.driver.session(database="neo4j") as session:
        session.run(
            "MATCH (a:FactoryNode {project: $project})-[r:SENDS_TO]->"
            "(b:FactoryNode {project: $project}) "
            "CREATE (a)-[old:FEEDS]->(b) SET old = properties(r) "
            "SET a:Asset REMOVE a:Equipment DELETE r",
            project=projection.project,
        ).consume()
    projection.publish(bundle)
    assert projection.summary() == expected
    with projection.driver.session(database="neo4j") as session:
        row = session.run(
            "MATCH (a:Equipment {project: $project, id: 'CV-01'})-[r:SENDS_TO]->"
            "(b:Equipment {id: 'LB-01'}) RETURN labels(a) AS labels, r.evidence_json AS evidence",
            project=projection.project,
        ).single()
        assert "Asset" not in row["labels"]
        assert json.loads(row["evidence"])[0]["source_id"] == "E1"
        assert (
            session.run(
                "MATCH (:FactoryNode {project: $project})-[r:FEEDS]->() RETURN count(r) AS count",
                project=projection.project,
            ).single()["count"]
            == 0
        )
