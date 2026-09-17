"""为素材图与关键帧保存 Provider 返回的原始图片 URL

Revision ID: 20260814_0014
Revises: 20260814_0013
Create Date: 2026-08-14

天悦视频要求 image_urls 传公网 http/https 地址。生图时 Provider 返回的
CDN 地址需要随本地文件一起保存，视频生成时优先使用该地址。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0014"
down_revision: str | None = "20260814_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "asset_image" in tables and not _has_column("asset_image", "provider_url"):
        op.add_column(
            "asset_image",
            sa.Column("provider_url", sa.Text(), nullable=True),
        )
    if "keyframe_version" in tables and not _has_column(
        "keyframe_version",
        "provider_url",
    ):
        op.add_column(
            "keyframe_version",
            sa.Column("provider_url", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("asset_image", "provider_url"):
        op.drop_column("asset_image", "provider_url")
    if _has_column("keyframe_version", "provider_url"):
        op.drop_column("keyframe_version", "provider_url")
