"""OpenAI embeddings + cosine similarity helpers.

`embed(text)` calls OpenAI text-embedding-3-small (1536 dims).
`cosine_similarity(a, b)` is a tiny numpy implementation used to compare
freshly-computed embeddings before persistence (the production lookup
goes through pgvector's <=> operator — see db.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from openai import AsyncOpenAI

from src.config import settings


async def embed(text: str) -> list[float]:
    """Return the embedding vector for `text`. Raises if OPENAI_API_KEY missing."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set; cannot compute embedding")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    result = await client.embeddings.create(
        model=settings.embedding_model,
        input=text,
    )
    return list(result.data[0].embedding)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 when either vector is zero."""
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(np.dot(va, vb) / denom)
