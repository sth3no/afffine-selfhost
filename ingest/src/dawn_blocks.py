"""Translate ingest's block specs into DAWNCR0W `append_block` arguments.

The renderers build blocks in the shape mcp_ext's `append_blocks` used to
accept (see `markdown_render.py`): `{"type": "paragraph", "style": "h2",
"text": InlineOp[]}`, `{"type": "embed-youtube", "url": ...}` and so on, with
InlineOp = str | {text, bold?, italic?, underline?, strike?, code?, link?,
refDocId?}. DAWNCR0W's `append_block` takes one block per call with canonical
types (`heading`, `embed_youtube`, ...) and Quill-style deltas for rich text.
"""

from __future__ import annotations

from typing import Any

_MARKS = ("bold", "italic", "underline", "strike", "code", "link")

_EMBEDS_BY_URL = {
    "embed-youtube": "embed_youtube",
    "embed-github": "embed_github",
    "embed-figma": "embed_figma",
    "embed-loom": "embed_loom",
    "embed-iframe": "embed_iframe",
}
_EMBEDS_BY_DOC = {"embed-linked-doc": "embed_linked_doc", "embed-synced-doc": "embed_synced_doc"}


def to_delta(text: str | list[Any]) -> str | list[dict[str, Any]]:
    """InlineOp[] -> delta array; a plain string stays a plain string.

    A `refDocId` op becomes a LinkedPage reference, which DAWNCR0W only accepts
    as a single-space insert (AFFiNE resolves the visible label itself)."""
    if isinstance(text, str):
        return text
    delta: list[dict[str, Any]] = []
    for op in text:
        if isinstance(op, str):
            delta.append({"insert": op})
        elif op.get("refDocId"):
            delta.append({"insert": " ", "attributes": {"reference": {"type": "LinkedPage", "pageId": op["refDocId"]}}})
        else:
            attrs = {k: op[k] for k in _MARKS if op.get(k)}
            delta.append({"insert": op["text"], **({"attributes": attrs} if attrs else {})})
    return delta


def to_append_block_args(spec: dict[str, Any]) -> dict[str, Any]:
    """One ingest block spec -> `append_block` arguments without `docId`/`placement`.

    Raises ValueError for a block type DAWNCR0W cannot take."""
    kind = spec.get("type")
    if kind == "paragraph":
        style = spec.get("style") or "text"
        text = to_delta(spec.get("text", ""))
        if style == "text":
            return {"type": "paragraph", "text": text}
        if style == "quote":
            return {"type": "quote", "text": text}
        return {"type": "heading", "level": int(style[1:]), "text": text}
    if kind == "list":
        args: dict[str, Any] = {"type": "list", "style": spec.get("style", "bulleted"), "text": to_delta(spec.get("text", ""))}
        if "checked" in spec:
            args["checked"] = bool(spec["checked"])
        return args
    if kind == "code":
        return {"type": "code", **({"language": spec["language"]} if spec.get("language") else {}), "text": spec.get("text", "")}
    if kind == "callout":
        # DAWNCR0W's callout has no emoji parameter; the body is what matters.
        return {"type": "callout", "text": to_delta(spec.get("text", ""))}
    if kind == "divider":
        return {"type": "divider"}
    if kind == "latex":
        return {"type": "latex", "latex": spec["latex"]}
    if kind == "bookmark":
        args = {"type": "bookmark", "url": spec["url"]}
        if spec.get("style"):
            args["bookmarkStyle"] = spec["style"]
        if spec.get("caption"):
            args["caption"] = spec["caption"]
        return args
    if kind in _EMBEDS_BY_URL:
        return {"type": _EMBEDS_BY_URL[kind], "url": spec["url"], **({"caption": spec["caption"]} if spec.get("caption") else {})}
    if kind in _EMBEDS_BY_DOC:
        return {"type": _EMBEDS_BY_DOC[kind], "pageId": spec["docId"]}
    if kind == "embed-html":
        return {"type": "embed_html", "html": spec["html"]}
    if kind == "image":
        return {"type": "image", "sourceId": spec["sourceId"], **({"caption": spec["caption"]} if spec.get("caption") else {})}
    raise ValueError(f"unsupported block type for append_block: {kind!r}")
