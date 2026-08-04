from __future__ import annotations

from scripts.arv001.full_pre_provider_canonical import (
    _canonical_private_staging_root,
)


def test_canonical_private_staging_accepts_symlinked_ancestor(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    real_parent = tmp_path / "real-temp"
    real_parent.mkdir()
    alias = tmp_path / "tmp-alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    staging, final = _canonical_private_staging_root(
        alias / "acceptance-root",
        repository,
    )

    assert staging is not None and final is not None
    assert staging.parent == (real_parent / "acceptance-root").resolve()
    assert final.parent == staging.parent
    assert staging.stat().st_mode & 0o777 == 0o700


def test_canonical_private_staging_rejects_leaf_symlink(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    alias = tmp_path / "root-alias"
    alias.symlink_to(real_root, target_is_directory=True)

    assert _canonical_private_staging_root(alias, repository) == (None, None)


def test_canonical_private_staging_rejects_resolved_repository_path(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repository, target_is_directory=True)

    assert _canonical_private_staging_root(
        alias / "private",
        repository,
    ) == (None, None)
