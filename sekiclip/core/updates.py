"""Optional release check (opt-in only, never automatic without preference).

Does not upload media. Only reads a public version document if the user enables
checks and a URL is configured.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from sekiclip import __version__

# Public JSON or plain-text version endpoint (set when you publish releases).
# Leave empty until a stable URL exists — check then reports "not configured".
DEFAULT_UPDATE_URL = ""

# Optional: GitHub latest release API (example — only used if set in prefs)
# "https://api.github.com/repos/OWNER/REPO/releases/latest"


@dataclass
class UpdateResult:
    ok: bool
    current: str
    remote: str | None = None
    newer: bool = False
    message: str = ""
    url: str | None = None


def _normalize_ver(v: str) -> tuple[int, ...]:
    v = (v or "").strip().lstrip("vV")
    # 0.1.0-beta.1 → 0.1.0
    v = re.split(r"[-+]", v, maxsplit=1)[0]
    parts: list[int] = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(remote: str, current: str = __version__) -> bool:
    return _normalize_ver(remote) > _normalize_ver(current)


def parse_remote_payload(body: str, content_type: str = "") -> tuple[str | None, str | None]:
    """Return (version, html_url) from JSON release API or plain text."""
    text = (body or "").strip()
    if not text:
        return None, None
    ct = (content_type or "").lower()
    if "json" in ct or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                tag = str(data.get("tag_name") or data.get("version") or "").strip()
                url = str(data.get("html_url") or data.get("url") or "").strip() or None
                return (tag or None), url
        except json.JSONDecodeError:
            pass
    # plain: first line is version
    line = text.splitlines()[0].strip()
    return (line or None), None


def check_for_update(
    *,
    url: str | None = None,
    timeout: float = 8.0,
) -> UpdateResult:
    """Fetch remote version. Call only when user opted in."""
    current = __version__
    endpoint = (url or DEFAULT_UPDATE_URL or "").strip()
    if not endpoint:
        return UpdateResult(
            ok=False,
            current=current,
            message="No update URL configured yet. Releases can set this in Settings.",
        )
    try:
        req = urllib.request.Request(
            endpoint,
            headers={
                "User-Agent": f"Sekiclip/{current} (update-check; offline app)",
                "Accept": "application/json, text/plain, */*",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type") or ""
        remote, page = parse_remote_payload(raw, ctype)
        if not remote:
            return UpdateResult(
                ok=False,
                current=current,
                message="Could not read remote version.",
                url=endpoint,
            )
        newer = is_newer(remote, current)
        if newer:
            msg = f"A newer version is available: {remote} (you have {current})."
        else:
            msg = f"You are up to date ({current})."
        return UpdateResult(
            ok=True,
            current=current,
            remote=remote,
            newer=newer,
            message=msg,
            url=page or endpoint,
        )
    except urllib.error.URLError as exc:
        return UpdateResult(
            ok=False,
            current=current,
            message=f"Could not reach update server: {exc.reason}",
            url=endpoint,
        )
    except Exception as exc:  # noqa: BLE001
        return UpdateResult(
            ok=False,
            current=current,
            message=f"Update check failed: {exc}",
            url=endpoint,
        )
