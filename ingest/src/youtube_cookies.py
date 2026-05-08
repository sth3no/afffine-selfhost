"""YouTube cookies storage — atomic write + Netscape format validation.

The browser-extension uploads a Netscape-format cookies.txt file via
POST /youtube/cookies. This module owns the on-disk persistence:
atomic write (so concurrent extractor reads never see a half-written
file), 0600 perms, and a quick format check that rejects garbage.

The cookies file is consumed by:
  - yt-dlp metadata fetch (`--cookies <path>`)
  - youtube-transcript-api (`YouTubeTranscriptApi(cookie_path=...)`)
  - cobalt service (env var `COOKIE_PATH`)

All three tolerate Netscape format. We never log the cookie body.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class InvalidCookieFile(ValueError):
    """Raised when the uploaded body doesn't look like a Netscape cookies.txt."""


def write_cookies_atomic(content: str, dest: Path) -> None:
    """Write `content` to `dest` atomically with mode 0o644.

    Atomic via os.replace — a concurrent reader either sees the old file or
    the new one, never a half-written one. Parent dirs are created if
    missing (the production tmpfs mount is `/run/cookies`, exists at
    container start).

    Mode is 0o644 (world-readable) on purpose: the cobalt container reads
    the cookies file as a non-root user, and ingest writes as a different
    user. Mode 0o600 silently broke cobalt's fs.readFile — every YT
    capture hit error.api.youtube.login because cobalt couldn't open the
    file. The volume is tmpfs internal to the docker compose network; the
    "extra protection" of 0o600 was protecting against nothing while
    actively breaking the consumer.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o644)
    except OSError:
        # Some filesystems (Windows during dev) don't honor chmod —
        # don't fail the write over it.
        pass
    os.replace(tmp, dest)


def validate_netscape(content: str) -> tuple[bool, str | None]:
    """Quick format check. Returns (ok, error_msg).

    Netscape format: header comment + tab-separated 7-field rows
    `domain  include_subdomains  path  secure  expires  name  value`.
    Empty lines and `#` comments are ignored.
    """
    rows = [
        line for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not rows:
        return False, "no cookie rows"
    for line in rows:
        if line.count("\t") != 6:
            return False, f"row has {line.count(chr(9))} tabs, expected 6"
    return True, None


def cookie_file_exists(path: str | Path) -> bool:
    """True iff the cookie file is present and non-empty."""
    p = Path(path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def netscape_to_cobalt_json(content: str) -> str:
    """Convert a Netscape cookies.txt body to cobalt v11's JSON format.

    Cobalt v11 expects:
        {"youtube": ["k1=v1; k2=v2", "k1=v1; k2=v2"], ...}
    where each array entry is one serialized cookie set. yt-dlp + the
    transcript-api consume the Netscape file directly; cobalt does not.

    We emit a single entry containing every YouTube + Google cookie row
    joined `name=value; name=value`. One entry is correct for cobalt's
    rotation logic — multiple entries are only useful when you have
    multiple accounts to round-robin between.

    Reference: https://github.com/imputnet/cobalt/blob/main/docs/examples/cookies.example.json
    """
    import json

    pairs: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        # Netscape row: domain include_subdomains path secure expires name value
        name, value = fields[5], fields[6]
        if name and value:
            pairs.append(f"{name}={value}")

    if not pairs:
        return json.dumps({})

    return json.dumps({"youtube": ["; ".join(pairs)]})


def cookie_file_status(path: str | Path) -> dict:
    """Return JSON-serializable freshness metadata. Never returns cookie content.

    Used by the read-only GET /youtube/cookies/status endpoint so the
    extension popup can render server-side staleness — the browser-side
    `lastSync` lies if the ingest container restarted and dropped tmpfs
    while the user wasn't browsing YouTube.
    """
    p = Path(path)
    try:
        st = p.stat()
    except (FileNotFoundError, OSError):
        return {"exists": False, "age_seconds": None, "mtime": None, "byte_count": 0}

    if not p.is_file() or st.st_size == 0:
        return {"exists": False, "age_seconds": None, "mtime": None, "byte_count": 0}

    mtime_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    age_seconds = max(0, int(datetime.now(tz=timezone.utc).timestamp() - st.st_mtime))
    mtime_iso = mtime_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "exists": True,
        "age_seconds": age_seconds,
        "mtime": mtime_iso,
        "byte_count": st.st_size,
    }
