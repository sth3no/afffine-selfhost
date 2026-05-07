"""Wire types for the classifier output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClassificationResult(BaseModel):
    """What the classifier returns for one Extracted record.

    `topic` may be None when the classifier is not confident enough to assign
    one; in that case `alias_of` is also None and the doc lands at the
    platform root (Phase 8 reorganizer revisits).

    `alias_of` lets the model propose a topic name that should collapse to
    an existing sibling (e.g., topic="Cooking", alias_of="Recipes") without
    creating a duplicate folder. The filer respects this directly without
    a separate embedding lookup.
    """

    model_config = ConfigDict(extra="ignore")

    topic: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    alias_of: str | None = None

    @field_validator("topic", mode="before")
    @classmethod
    def _normalize_topic(cls, value):
        if value is None:
            return None
        s = str(value).strip()
        return s if s else None
