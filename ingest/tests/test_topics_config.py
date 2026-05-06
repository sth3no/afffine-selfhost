from pathlib import Path

import pytest

from src.config import TopicsConfig, load_topics


def test_load_topics_parses_real_file():
    """Smoke: the bundled topics.yaml must be parseable with no errors."""
    config = load_topics()
    assert isinstance(config, TopicsConfig)
    assert len(config.platforms) >= 5
    # The catch-all `article` platform must be present and last.
    assert config.platforms[-1].id == "article"
    assert config.platforms[-1].hosts == ["*"]


def test_load_topics_from_explicit_path(tmp_path: Path):
    yaml_text = """
platforms:
  - id: example
    group: Articles
    folder_name: Example
    hosts: [example.com]
    extractor: markitdown
"""
    p = tmp_path / "topics.yaml"
    p.write_text(yaml_text)

    config = load_topics(p)

    assert len(config.platforms) == 1
    plat = config.platforms[0]
    assert plat.id == "example"
    assert plat.group == "Articles"
    assert plat.folder_name == "Example"
    assert plat.hosts == ["example.com"]
    assert plat.extractor == "markitdown"


def test_load_topics_tolerates_missing_optional_sections(tmp_path: Path):
    yaml_text = """
platforms:
  - id: only
    group: Articles
    folder_name: Only
    hosts: ["*"]
    extractor: markitdown
"""
    p = tmp_path / "topics.yaml"
    p.write_text(yaml_text)

    config = load_topics(p)

    assert config.topic_hints == {}
    assert config.reorg.default_threshold == 15
    assert config.reorg.overrides == {}


def test_load_topics_fails_loudly_on_no_platforms(tmp_path: Path):
    p = tmp_path / "topics.yaml"
    p.write_text("platforms: []\n")
    with pytest.raises(ValueError, match="at least one platform"):
        load_topics(p)
