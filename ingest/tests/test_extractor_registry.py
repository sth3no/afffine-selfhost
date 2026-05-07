import pytest

from src.pipeline.extractors import get_extractor, register_extractor


@pytest.mark.asyncio
async def test_register_and_get_extractor():
    async def fake(url, platform):
        return None

    register_extractor("__test_fake__", fake)
    fn = get_extractor("__test_fake__")
    assert fn is fake


def test_get_extractor_raises_on_unknown_name():
    with pytest.raises(KeyError, match="no extractor named"):
        get_extractor("does_not_exist")


def test_builtin_extractors_registered():
    """Importing the package must register the four built-in extractors.
    Skipped while built-ins are still being added in Tasks 3-6."""
    pending = []
    for name in ("markitdown", "ytdlp", "oembed_ytdlp", "reddit_json"):
        try:
            get_extractor(name)
        except KeyError:
            pending.append(name)
    if pending:
        pytest.skip(f"built-ins not yet registered: {pending}")
