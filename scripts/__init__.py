"""Repository-local operational scripts package.

Keeping ``scripts`` as an explicit package ensures commands executed from a Git
worktree import that worktree's modules instead of an editable installation
from another checkout of the same repository.
"""
