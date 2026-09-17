"""为动态工作流任务增加查询与幂等索引。

Revision ID: 20260816_0016_dynamic_job_idempotency
Revises: 20260816_0015
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_0016_dynamic_job_idempotency"
down_revision: str | None = "20260816_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names()


def _has_index(table: str, index: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == index for item in inspector.get_indexes(table))


def _create_index(
    name: str,
    columns: list[str],
    *,
    unique: bool = False,
    non_empty: bool = False,
) -> None:
    if not _has_table("job") or _has_index("job", name):
        return

    if non_empty:
        predicate = sa.text("idempotency_key IS NOT NULL AND idempotency_key <> ''")
        if op.get_bind().dialect.name == "sqlite":
            op.create_index(name, "job", columns, unique=unique, sqlite_where=predicate)
            return
        if op.get_bind().dialect.name == "postgresql":
            op.create_index(name, "job", columns, unique=unique, postgresql_where=predicate)
            return

    op.create_index(name, "job", columns, unique=unique)


def _drop_index(name: str) -> None:
    if _has_table("job") and _has_index("job", name):
        op.drop_index(name, table_name="job")


def upgrade() -> None:
    _create_index(
        "idx_job_workflow_run_status",
        ["workflow_run_id", "status"],
    )
    _create_index(
        "idx_job_workflow_stage_status",
        ["workflow_stage_id", "status"],
    )
    _create_index(
        "ux_job_idempotency_key",
        ["idempotency_key"],
        unique=True,
        non_empty=True,
    )


def downgrade() -> None:
    _drop_index("ux_job_idempotency_key")
    _drop_index("idx_job_workflow_stage_status")
    _drop_index("idx_job_workflow_run_status")
