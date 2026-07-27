from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from src.modules.production_llm_analysis.document_recovery import (
    MINIMUM_REQUIRED_ALEMBIC_REVISION,
    _report,
    _schema_gate_details,
    _schema_graph_state,
)


class Revision:
    def __init__(self, revision: str, down_revision: str | tuple[str, ...] | None):
        self.revision = revision
        self.down_revision = down_revision


class Graph:
    def __init__(self, heads: tuple[str, ...], revisions: dict[str, Revision]):
        self._heads = heads
        self._revisions = revisions

    def get_heads(self):
        return self._heads

    def get_revision(self, revision):
        if revision not in self._revisions:
            raise KeyError(revision)
        return self._revisions[revision]


def graph(*, head: str = "097", minimum: str = "096", minimum_parent: str = "095"):
    return Graph(
        (head,),
        {
            minimum: Revision(minimum, minimum_parent),
            head: Revision(head, minimum),
            minimum_parent: Revision(minimum_parent, None),
        },
    )


@pytest.mark.parametrize(
    ("heads", "revisions", "expected"),
    [
        (
            (MINIMUM_REQUIRED_ALEMBIC_REVISION,),
            (MINIMUM_REQUIRED_ALEMBIC_REVISION,),
            True,
        ),
        (("097",), ("097",), True),
        (("098",), ("098",), True),
        (("097",), (MINIMUM_REQUIRED_ALEMBIC_REVISION,), False),
        (("097",), ("unknown",), False),
        (("097",), ("098",), False),
        (("097", "side"), ("097",), False),
        (("097",), (MINIMUM_REQUIRED_ALEMBIC_REVISION, "097"), False),
        (("097",), (), False),
    ],
)
def test_schema_graph_gate_cases(heads, revisions, expected):
    revisions_map = {
        "095": Revision("095", None),
        MINIMUM_REQUIRED_ALEMBIC_REVISION: Revision(
            MINIMUM_REQUIRED_ALEMBIC_REVISION, "095"
        ),
        "097": Revision("097", MINIMUM_REQUIRED_ALEMBIC_REVISION),
        "098": Revision("098", MINIMUM_REQUIRED_ALEMBIC_REVISION),
        "side": Revision("side", "095"),
    }
    result = _schema_graph_state(
        Graph(heads, revisions_map),
        tuple(revisions),
    )
    assert result["ready"] is expected


def test_schema_graph_rejects_divergent_branch():
    result = _schema_graph_state(
        Graph(
            ("098",),
            {
                "095": Revision("095", None),
                MINIMUM_REQUIRED_ALEMBIC_REVISION: Revision(
                    MINIMUM_REQUIRED_ALEMBIC_REVISION, "095"
                ),
                "098": Revision("098", (MINIMUM_REQUIRED_ALEMBIC_REVISION, "side")),
                "side": Revision("side", "095"),
            },
        ),
        ("098",),
    )
    assert result["ready"] is False
    assert result["alembic_minimum_is_ancestor"] is False


def test_schema_graph_rejects_revision_resolution_error():
    class BrokenGraph(Graph):
        def get_revision(self, revision):
            if revision == "097":
                raise RuntimeError("broken graph")
            return super().get_revision(revision)

    result = _schema_graph_state(BrokenGraph(("097",), graph()._revisions), ("097",))
    assert result["ready"] is False


def test_real_repository_graph_accepts_current_head_and_rejects_minimum_only(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(255))")
        )
        connection.execute(
            text(
                "INSERT INTO alembic_version (version_num) VALUES ('097_add_arv052_expert_review')"
            )
        )
    current = _schema_gate_details(engine)
    assert current["ready"] is True
    assert current["alembic_repository_head"] == "097_add_arv052_expert_review"
    assert (
        current["alembic_minimum_required_revision"]
        == MINIMUM_REQUIRED_ALEMBIC_REVISION
    )
    assert current["alembic_minimum_present"] is True
    assert current["alembic_minimum_is_ancestor"] is True
    assert current["alembic_database_at_repository_head"] is True
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text(
                "INSERT INTO alembic_version (version_num) VALUES ('096_add_r8_canonical_snapshot_binding')"
            )
        )
    stale = _schema_gate_details(engine)
    assert stale["ready"] is False
    engine.dispose()


def test_schema_report_fields_are_sanitized():
    report = _report(
        alembic_repository_head="097_add_arv052_expert_review",
        alembic_minimum_present=True,
        alembic_minimum_is_ancestor=True,
        alembic_database_at_repository_head=True,
    )
    serialized = json.dumps(report, sort_keys=True)
    assert "postgresql://" not in serialized
    assert "/migrations/" not in serialized
