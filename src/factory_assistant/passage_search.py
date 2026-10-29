"""Search source passages by words and, optionally, cached embedding similarity.

The caller supplies only date/dataset/applicability-filtered passages. An embedding
cache never grants access to a passage: access is checked before ranking on every
query. Similarity is a retrieval signal, never evidence of applicability or truth.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path

from openai import OpenAI

from factory_assistant.answer_models import EvidenceCard

STOP_WORDS = set(
    "a an the of to in on for is are was were and or it this that what how why".split()
)


def tokens(text: str) -> set[str]:
    """Small, domain-neutral lexical baseline; identifiers retain their hyphens."""
    return set(re.findall(r"[\w-]+", text.casefold())) - STOP_WORDS


class EmbeddingCache:
    """Persist model-specific vectors; batch only previously unseen source/query text."""

    def __init__(self, path: Path, client=None, model: str | None = None):
        self.path = path
        self.model = model or os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        self.client = client or OpenAI(timeout=45, max_retries=1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS embeddings "
                "(model TEXT, digest TEXT, vector TEXT, PRIMARY KEY(model, digest))"
            )

    def vectors(self, texts: list[str]) -> list[list[float]]:
        """Return vectors in input order; never log credentials or send unrelated files."""
        keys = [hashlib.sha256(text.encode()).hexdigest() for text in texts]
        cached = {}
        with sqlite3.connect(self.path) as db:
            for key in set(keys):
                row = db.execute(
                    "SELECT vector FROM embeddings WHERE model=? AND digest=?", (self.model, key)
                ).fetchone()
                if row:
                    cached[key] = json.loads(row[0])
            missing = {
                key: text for key, text in zip(keys, texts, strict=True) if key not in cached
            }
            items = list(missing.items())
            for start in range(0, len(items), 64):
                batch = items[start : start + 64]
                response = self.client.embeddings.create(
                    model=self.model, input=[text for _, text in batch], encoding_format="float"
                )
                indexed = {entry.index: entry.embedding for entry in response.data}
                if set(indexed) != set(range(len(batch))):
                    raise ValueError("Embedding response omitted an input.")
                for i, (key, _) in enumerate(batch):
                    vector = indexed[i]
                    if not vector or not all(math.isfinite(x) for x in vector):
                        raise ValueError("Invalid embedding vector.")
                    cached[key] = vector
                    db.execute(
                        "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?)",
                        (self.model, key, json.dumps(vector)),
                    )
        return [cached[key] for key in keys]


def search_passages(
    query: str, cards: list[EvidenceCard], limit: int, embeddings: EmbeddingCache | None = None
) -> list[dict]:
    """Combine lexical and semantic ranks; keep scores inspectable, not 'confidence'."""
    if not cards:
        return []
    words = tokens(query)
    lexical = {c.id: len(words & tokens(c.title + " " + c.text)) for c in cards}
    lexical_order = sorted(
        (c.id for c in cards if lexical[c.id]), key=lambda key: (-lexical[key], key)
    )
    semantic, semantic_order = {}, []
    if embeddings is not None:
        vectors = embeddings.vectors([query, *[c.title + "\n" + c.text for c in cards]])
        query_vector = vectors[0]
        for card, vector in zip(cards, vectors[1:], strict=True):
            if len(vector) != len(query_vector):
                raise ValueError("Embedding dimensions do not match.")
            denominator = math.sqrt(sum(x * x for x in vector) * sum(x * x for x in query_vector))
            semantic[card.id] = (
                sum(a * b for a, b in zip(query_vector, vector, strict=True)) / denominator
                if denominator
                else 0
            )
        semantic_order = sorted(semantic, key=lambda key: (-semantic[key], key))
    scores = {}
    for ranking in (lexical_order, semantic_order):
        for position, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + position)
    by_id = {c.id: c for c in cards}
    return [
        {
            "card": by_id[key].model_dump(),
            "lexical_matches": lexical[key],
            "cosine_similarity": semantic.get(key),
            "rank_score": scores[key],
        }
        for key in sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    ]
