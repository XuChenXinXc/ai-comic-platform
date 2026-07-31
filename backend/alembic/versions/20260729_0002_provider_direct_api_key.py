"""Provider 直接 API Key 密文

Revision ID: 20260729_0002
Revises: 20260728_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260729_0002"
down_revision: str | None = "20260728_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加 Provider 直接 API Key 的 DPAPI 密文列。"""
    op.add_column("api_provider", sa.Column("api_key_ciphertext", sa.Text(), nullable=True))


def downgrade() -> None:
    """移除 Provider 直接 API Key 的 DPAPI 密文列。"""
    op.drop_column("api_provider", "api_key_ciphertext")
