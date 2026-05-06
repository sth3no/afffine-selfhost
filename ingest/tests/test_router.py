from src.config import Platform, TopicsConfig
from src.pipeline.router import PlatformRouter


def _config() -> TopicsConfig:
    return TopicsConfig(
        platforms=[
            Platform(id="youtube", group="Socials", folder_name="Youtube",
                     hosts=["youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"],
                     extractor="ytdlp"),
            Platform(id="instagram", group="Socials", folder_name="Instagram",
                     hosts=["instagram.com", "www.instagram.com"],
                     extractor="ytdlp"),
            Platform(id="x", group="Socials", folder_name="X",
                     hosts=["x.com", "twitter.com", "www.x.com"],
                     extractor="oembed_ytdlp"),
            Platform(id="arxiv", group="Research papers", folder_name="arXiv",
                     hosts=["arxiv.org"],
                     extractor="markitdown"),
            Platform(id="article", group="Articles", folder_name="Web",
                     hosts=["*"],
                     extractor="markitdown"),
        ],
    )


URL_CASES = [
    # YouTube variants
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtube.com/watch?v=abc", "youtube"),
    ("https://m.youtube.com/watch?v=abc", "youtube"),
    ("https://youtu.be/abc", "youtube"),
    ("https://youtube.com/shorts/xyz", "youtube"),
    # Instagram
    ("https://www.instagram.com/p/Cxyz123/", "instagram"),
    ("https://instagram.com/reel/Czzzz/", "instagram"),
    # X / Twitter
    ("https://x.com/anyuser/status/123", "x"),
    ("https://twitter.com/anyuser/status/123", "x"),
    # arXiv
    ("https://arxiv.org/abs/2401.00001", "arxiv"),
    ("http://arxiv.org/pdf/2401.00001v1.pdf", "arxiv"),
    # Catch-all
    ("https://en.wikipedia.org/wiki/Foo", "article"),
    ("https://news.ycombinator.com/item?id=1", "article"),
    ("https://blog.example.com/post", "article"),
    ("http://random.local/page", "article"),
    # Edge: bare host without scheme upgraded to https? (URL parse should still work)
    ("https://example.com", "article"),
    # Edge: URL with port
    ("https://example.com:8080/page", "article"),
    # Trailing slash variants
    ("https://www.instagram.com/p/Cxyz/", "instagram"),
    ("https://www.instagram.com/p/Cxyz", "instagram"),
    # Subdomain not in list falls through to catch-all
    ("https://api.youtube.com/v3/...", "article"),  # api.youtube.com NOT in list
]


import pytest


@pytest.mark.parametrize("url,expected_id", URL_CASES)
def test_router_resolves(url: str, expected_id: str):
    router = PlatformRouter(_config())
    plat = router.detect(url)
    assert plat.id == expected_id, f"{url} -> got {plat.id}, want {expected_id}"


def test_router_returns_full_platform_object():
    router = PlatformRouter(_config())
    plat = router.detect("https://www.instagram.com/p/Cxyz/")
    assert plat.group == "Socials"
    assert plat.folder_name == "Instagram"
    assert plat.extractor == "ytdlp"


def test_router_initial_path_helper():
    router = PlatformRouter(_config())
    plat = router.detect("https://www.instagram.com/p/Cxyz/")
    assert router.initial_path(plat) == ["Sources", "Socials", "Instagram"]


def test_router_no_catch_all_raises():
    """A config without the wildcard entry is a misconfiguration —
    surface it loudly rather than silently dropping URLs."""
    bad = TopicsConfig(platforms=[
        Platform(id="only", group="Socials", folder_name="Only",
                 hosts=["specific.example.com"], extractor="markitdown"),
    ])
    router = PlatformRouter(bad)
    import pytest
    with pytest.raises(LookupError, match="no catch-all"):
        router.detect("https://other.com/page")


def test_router_invalid_url_raises():
    router = PlatformRouter(_config())
    import pytest
    with pytest.raises(ValueError, match="cannot extract host"):
        router.detect("not-a-url")
