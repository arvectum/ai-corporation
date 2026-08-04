from pathlib import Path


def test_arv001_make_target_uses_configurable_python_311() -> None:
    makefile = (Path(__file__).resolve().parents[2] / "Makefile").read_text(
        encoding="utf-8"
    )

    assert "PYTHON ?= python" in makefile
    assert "@$(PYTHON) -c 'import sys;" in makefile
    assert "sys.version_info[:2] == (3, 11)" in makefile
    assert (
        "@$(PYTHON) -m scripts.arv001.full_pre_provider_canonical" in makefile
    )
    assert "@$(PYTHON) -m scripts.arv001.full_pre_provider \\" not in makefile
    assert "@python -m scripts.arv001.full_pre_provider" not in makefile
