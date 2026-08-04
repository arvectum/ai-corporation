from __future__ import annotations

from pathlib import Path

import scripts.arv001.full_pre_provider as full_pre_provider


def test_full_pre_provider_is_imported_from_active_repository() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    module_root = Path(full_pre_provider.__file__).resolve().parents[2]

    assert module_root == repository_root
