"""Start/stop this project's local Neo4j without printing or committing its password."""

import argparse
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["start", "stop"])
args = parser.parse_args()
settings = ROOT / ".runtime/neo4j.env"
if not settings.exists():
    settings.parent.mkdir(exist_ok=True)
    # Exclusive creation and owner-only permissions avoid exposing a generated password.
    with settings.open("x") as stream:
        settings.chmod(0o600)
        stream.write(
            "NEO4J_URI=bolt://127.0.0.1:7687\nNEO4J_USER=neo4j\n"
            f"NEO4J_PASSWORD={secrets.token_urlsafe(24)}\n"
        )
command = ["docker", "compose", "--env-file", str(settings), "-f", str(ROOT / "compose.yaml")]
command += ["up", "-d"] if args.action == "start" else ["stop"]
subprocess.run(command, cwd=ROOT, check=True)
print(
    "Local graph started; allow a few seconds for startup."
    if args.action == "start"
    else "Local graph stopped. Its data is retained."
)
