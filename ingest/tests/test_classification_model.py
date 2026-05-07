import pytest
from pydantic import ValidationError

from src.pipeline.classification import ClassificationResult


def test_classification_result_minimal():
    r = ClassificationResult(topic="Recipes", confidence=0.92, reasoning="dish photo")
    assert r.topic == "Recipes"
    assert r.confidence == 0.92
    assert r.alias_of is None


def test_classification_result_with_alias():
    r = ClassificationResult(topic="Cooking", confidence=0.85, reasoning="similar to Recipes", alias_of="Recipes")
    assert r.alias_of == "Recipes"


def test_classification_result_low_confidence_topic_can_be_null():
    r = ClassificationResult(topic=None, confidence=0.3, reasoning="ambiguous content")
    assert r.topic is None


def test_classification_result_rejects_confidence_out_of_range():
    with pytest.raises(ValidationError):
        ClassificationResult(topic="X", confidence=1.5, reasoning="bug")
    with pytest.raises(ValidationError):
        ClassificationResult(topic="X", confidence=-0.1, reasoning="bug")


def test_classification_result_strips_topic_whitespace():
    r = ClassificationResult(topic="  Recipes  ", confidence=0.9, reasoning="x")
    assert r.topic == "Recipes"


def test_classification_result_empty_topic_becomes_none():
    r = ClassificationResult(topic="", confidence=0.5, reasoning="x")
    assert r.topic is None
