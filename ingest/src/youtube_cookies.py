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
from pathlib import Path

log = logging.getLogger(__name__)


class InvalidCookieFile(ValueError):
    """Raised when the uploaded body doesn't look like a Netscape cookies.txt."""


def write_cookies_atomic(content: str, dest: Path) -> None:
    """Write `content` to `dest` atomically with mode 0o600.

    Atomic via os.rename — a concurrent reader either sees the old file or
    the new one, never a half-written one. Parent dirs are created if
    missing (the production tmpfs mount is `/run/cookies`, exists at
    container start).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
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
