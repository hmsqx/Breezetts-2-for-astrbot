"""Shared error decoding at the local cover API boundary."""

import json


def _backend_error_detail(detail: str) -> str:
    """Show the downloader's actual error instead of its leading progress log."""
    try:
        payload = json.loads(detail)
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
            detail = payload["detail"]
    except ValueError:
        pass
    errors = [line.strip() for line in detail.splitlines() if "ERROR:" in line]
    if errors:
        detail = errors[-1].split("; please report this issue", 1)[0]
    return detail.strip()[:600] or "后端未返回错误详情"
