from __future__ import annotations

from pathlib import Path

from src.shared.runtime import preflight


def test_preflight_does_not_create_missing_data_directory(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing-runtime-root"
    settings = type(
        "SettingsStub",
        (),
        {
            "arvectum_data_dir": str(missing),
            "pilot_auth_enabled": False,
            "pilot_auth_password_safe": lambda self: True,
            "local_llm_base_url": "http://127.0.0.1:1/v1",
            "rag_embeddings_base_url": "http://127.0.0.1:1/v1",
        },
    )()
    monkeypatch.setattr(preflight, "get_settings", lambda: settings)

    assert preflight.main() == 1
    assert not missing.exists()
