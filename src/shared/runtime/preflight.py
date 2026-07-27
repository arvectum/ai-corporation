"""Non-destructive runtime preflight for the Mac mini contour."""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.request import urlopen

from src.shared.config.settings import get_settings


def _reachable(url: str) -> bool:
    try:
        endpoint = url.rstrip("/") + "/models"
        with urlopen(endpoint, timeout=3) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def main() -> int:
    settings = get_settings()
    errors: list[str] = []
    data_dir = Path(settings.arvectum_data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    if not data_dir.is_dir() or not data_dir.stat():
        errors.append("data directory is unavailable")
    if shutil.disk_usage(data_dir).free < 2 * 1024**3:
        errors.append("less than 2 GiB free disk")
    if settings.pilot_auth_enabled and not settings.pilot_auth_password_safe():
        errors.append("pilot auth password is empty or placeholder")
    for name, url in (("LLM", settings.local_llm_base_url), ("embeddings", settings.rag_embeddings_base_url)):
        if not _reachable(url):
            errors.append(f"{name} endpoint is unreachable")
    if errors:
        print("preflight: failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("preflight: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
