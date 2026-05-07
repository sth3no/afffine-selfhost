from datetime import timezone
from unittest.mock import patch, MagicMock

import pytest

from src.config import Platform
from src.pipeline.extracted import MediaKind
from src.pipeline.extractors.reddit_json_ext import extract


def _platform() -> Platform:
    return Platform(id="reddit", group="Socials", folder_name="Reddit",
                    hosts=["reddit.com"], extractor="reddit_json")


SAMPLE = [
    {"data": {"children": [{
        "data": {
            "title": "Best recipe ever",
            "author": "u_chef",
            "selftext": "Here are the steps:\n\n1. Mix\n2. Bake",
            "subreddit": "cooking",
            "created_utc": 1746576000,
        }
    }]}},
    {"data": {"children": [
        {"data": {"author": "alice", "body": "Looks amazing"}},
        {"data": {"author": "bob", "body": "Does it freeze well?"}},
    ]}},
]


@pytest.mark.asyncio
async def test_reddit_extracts_post_and_comments():
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = SAMPLE
    fake_resp.raise_for_status = MagicMock()

    with patch("src.pipeline.extractors.reddit_json_ext.httpx.AsyncClient") as Client:
        ctx = Client.return_value.__aenter__.return_value
        ctx.get.return_value = fake_resp
        e = await extract("https://www.reddit.com/r/cooking/comments/abc/best/", _platform())

    assert e.title == "Best recipe ever"
    assert e.author == "u_chef"
    assert e.media_kind == MediaKind.TEXT
    assert "r/cooking" in e.body_md
    assert "Mix" in e.body_md
    assert "Looks amazing" in e.body_md
    assert "Does it freeze well?" in e.body_md
    assert e.published_at.tzinfo == timezone.utc
    assert e.extra["subreddit"] == "cooking"
