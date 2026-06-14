import math
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.pipeline.embeddings import cosine_similarity, embed


def test_cosine_similarity_identical_vectors_is_one():
    a = [0.1, 0.2, 0.3]
    assert math.isclose(cosine_similarity(a, a), 1.0, abs_tol=1e-9)


def test_cosine_similarity_orthogonal_is_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert math.isclose(cosine_similarity(a, b), 0.0, abs_tol=1e-9)


def test_cosine_similarity_opposite_is_minus_one():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert math.isclose(cosine_similarity(a, b), -1.0, abs_tol=1e-9)


def test_cosine_similarity_zero_vector_returns_zero():
    """Avoid div-by-zero; an undefined cosine is treated as 0 (no similarity)."""
    a = [0.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert cosine_similarity(a, b) == 0.0


@pytest.mark.asyncio
async def test_embed_calls_openai_with_correct_args(monkeypatch):
    fake_response = MagicMock()
    fake_response.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]

    monkeypatch.setattr(
        "src.pipeline.embeddings.settings",
        MagicMock(openai_api_key="sk-test", embedding_model="text-embedding-3-small"),
    )

    with patch("src.pipeline.embeddings.openai_client") as Client:
        instance = Client.return_value
        instance.embeddings.create = AsyncMock(return_value=fake_response)

        vec = await embed("Recipes")

    Client.assert_called_once()  # constructed with api_key
    instance.embeddings.create.assert_awaited_once()
    call = instance.embeddings.create.await_args
    assert call.kwargs["model"] == "text-embedding-3-small"
    assert call.kwargs["input"] == "Recipes"
    assert vec == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_raises_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("src.pipeline.embeddings.settings", MagicMock(openai_api_key="", embedding_model="x"))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await embed("anything")
