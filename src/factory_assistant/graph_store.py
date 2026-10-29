"""An optional, repeatable Neo4j projection of validated evidence.

SQLite remains the inventory review ledger. Graph publication is a separate atomic
transaction, not a distributed transaction. A failed projection can safely be retried.
"""

import json
import os
from pathlib import Path

from dotenv import dotenv_values
from neo4j import GraphDatabase

from factory_assistant.display import node_label
from factory_assistant.graph_models import EDGE_TYPES, GraphBundle, Node
from factory_assistant.graph_names import upgrade_database_names

PROJECT = "bosch-assignment"


def graph_settings(root: Path) -> dict:
    local = dotenv_values(root / ".runtime/neo4j.env")
    # A complete explicit configuration wins. Empty template values do not hide local setup.
    if os.environ.get("NEO4J_PASSWORD"):
        return {
            "uri": os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
            "user": os.environ.get("NEO4J_USER", "neo4j"),
            "password": os.environ["NEO4J_PASSWORD"],
        }
    if local.get("NEO4J_PASSWORD"):
        return {
            "uri": local["NEO4J_URI"],
            "user": local["NEO4J_USER"],
            "password": local["NEO4J_PASSWORD"],
        }
    raise ValueError("Start the local graph with tools/local_graph.py, or configure Neo4j.")


class Neo4jProjection:
    def __init__(self, root: Path, project: str = PROJECT):
        settings = graph_settings(root)
        self.project = project
        self.driver = GraphDatabase.driver(
            settings["uri"],
            auth=(settings["user"], settings["password"]),
            connection_timeout=5,
            max_transaction_retry_time=5,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.driver.close()

    def publish(self, bundle: GraphBundle) -> dict:
        bundle.validate_links()
        self.driver.verify_connectivity()
        with self.driver.session(database="neo4j") as session:
            session.run(
                "CREATE CONSTRAINT factory_node_key IF NOT EXISTS "
                "FOR (n:FactoryNode) REQUIRE n.key IS UNIQUE"
            ).consume()
            session.run(
                "CREATE CONSTRAINT factory_project_key IF NOT EXISTS "
                "FOR (n:FactoryProject) REQUIRE n.name IS UNIQUE"
            ).consume()
            return session.execute_write(self._write, bundle)

    def _write(self, tx, bundle):
        upgrade_database_names(tx, self.project)
        build = bundle.fingerprint
        prefix = f"{self.project}:{build}:"
        # Pydantic label literals and relation allowlist keep identifiers out of user input.
        kinds = Node.model_fields["kind"].annotation.__args__
        for kind in kinds:
            rows = [
                {
                    "key": prefix + n.id,
                    "id": n.id,
                    "properties": {**n.properties, "display_name": node_label(n)},
                    "evidence": json.dumps([r.model_dump() for r in n.evidence]),
                }
                for n in bundle.nodes
                if n.kind == kind
            ]
            if rows:
                tx.run(
                    f"UNWIND $rows AS row MERGE (n:FactoryNode:{kind} {{key: row.key}}) "
                    "SET n += row.properties, n.id = row.id, n.project = $project, "
                    "n.build = $build, n.evidence_json = row.evidence",
                    rows=rows,
                    project=self.project,
                    build=build,
                ).consume()
        for relation in EDGE_TYPES:
            rows = [
                {
                    "source": prefix + e.source,
                    "target": prefix + e.target,
                    "evidence": json.dumps([r.model_dump() for r in e.evidence]),
                }
                for e in bundle.edges
                if e.relation == relation
            ]
            if rows:
                tx.run(
                    "UNWIND $rows AS row MATCH (a:FactoryNode {key: row.source}), "
                    "(b:FactoryNode {key: row.target}) "
                    f"MERGE (a)-[r:{relation}]->(b) SET r.evidence_json = row.evidence",
                    rows=rows,
                ).consume()
        # Switch readers only once every node and relationship has been written successfully.
        tx.run(
            "MERGE (p:FactoryProject {name: $project}) SET p.active_build = $build",
            project=self.project,
            build=build,
        ).consume()
        return {"build": build, "nodes": len(bundle.nodes), "relationships": len(bundle.edges)}

    def summary(self) -> dict:
        with self.driver.session(database="neo4j") as session:
            rows = session.run(
                "MATCH (p:FactoryProject {name: $project}) "
                "MATCH (n:FactoryNode {project: $project, build: p.active_build}) "
                "OPTIONAL MATCH (n)-[r]->(:FactoryNode) "
                "RETURN p.active_build AS build, count(DISTINCT n) AS nodes, "
                "count(r) AS relationships",
                project=self.project,
            ).single()
            return dict(rows) if rows else {}
