"""multi chapter support

Revision ID: 20260806_0008
Revises: 20260805_0007
Create Date: 2026-08-06

引入章节维度：一个项目 = 一本小说，项目下多个章节，全书共用素材库。

- 新增 chapter 表，正文相关字段从 project 下沉到 chapter
  （project 上的旧列保留为无用列，见 upgrade 内注释）
- novel_analysis / screenplay 主键由 project_id 改为 chapter_id
- shot 新增 chapter_id，编号唯一性由项目内改为章节内
- job 新增可空 chapter_id

按用户要求不迁移历史数据：正文、分析、剧本、分镜及其媒体版本会被清空，
素材库（asset / asset_image）与项目配置保留。
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260806_0008"
down_revision: str | None = "20260805_0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "chapter",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chapter_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','ready','confirmed','failed')",
            name="ck_chapter_status",
        ),
        sa.UniqueConstraint("project_id", "chapter_no", name="uq_chapter_project_no"),
    )
    op.create_index("idx_chapter_project_no", "chapter", ["project_id", "chapter_no"])

    # 分镜及其媒体版本依赖 project 维度的编号与正文，结构变更后无法安全保留。
    # 从更早版本链式升级时部分表可能尚未创建，逐个判断存在性。
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in (
        "keyframe_version",
        "video_version",
        "shot",
        "novel_analysis",
        "screenplay",
        "job",
    ):
        if table_name in existing_tables:
            op.execute(f"DELETE FROM {table_name}")

    # novel_analysis / screenplay：主键换成 chapter_id
    for table_name in ("novel_analysis", "screenplay"):
        if table_name in existing_tables:
            op.drop_table(table_name)
    op.create_table(
        "novel_analysis",
        sa.Column(
            "chapter_id",
            sa.Text(),
            sa.ForeignKey("chapter.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("source_signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generated_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','generating','ready','confirmed','failed')",
            name="ck_novel_analysis_status",
        ),
    )
    op.create_table(
        "screenplay",
        sa.Column(
            "chapter_id",
            sa.Text(),
            sa.ForeignKey("chapter.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("source_signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("own_signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generated_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','generating','ready','confirmed','failed')",
            name="ck_screenplay_status",
        ),
    )

    if "shot" in existing_tables:
        with op.batch_alter_table("shot") as batch:
            batch.add_column(
                sa.Column(
                    "chapter_id",
                    sa.Text(),
                    sa.ForeignKey("chapter.id", ondelete="CASCADE"),
                    nullable=False,
                )
            )
            batch.drop_constraint("uq_shot_project_no", type_="unique")
            batch.create_unique_constraint("uq_shot_chapter_no", ["chapter_id", "shot_no"])
        op.drop_index("idx_shot_project_order", table_name="shot")
        op.create_index("idx_shot_chapter_order", "shot", ["chapter_id", "shot_order"])

    if "job" in existing_tables:
        with op.batch_alter_table("job") as batch:
            batch.add_column(
                sa.Column(
                    "chapter_id",
                    sa.Text(),
                    sa.ForeignKey("chapter.id", ondelete="CASCADE"),
                )
            )

    # 正文已下沉到 chapter，project 上的三列不再使用。
    # 这里刻意不 DROP：SQLite 的 batch_alter_table 会重建 project 表，
    # 而 PRAGMA foreign_keys 无法在迁移事务内关闭，重建时 ON DELETE CASCADE
    # 会连带清空 asset / asset_image，导致全书素材库丢失。
    # 保留为无用列的代价远小于误删素材库。
    project_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("project")
    }
    if {"source_text", "source_signature"} <= project_columns:
        op.execute("UPDATE project SET source_text = '', source_signature = ''")


def downgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.drop_column("chapter_id")

    op.drop_index("idx_shot_chapter_order", table_name="shot")
    op.create_index("idx_shot_project_order", "shot", ["project_id", "shot_order"])
    with op.batch_alter_table("shot") as batch:
        batch.drop_constraint("uq_shot_chapter_no", type_="unique")
        batch.create_unique_constraint("uq_shot_project_no", ["project_id", "shot_no"])
        batch.drop_column("chapter_id")

    for table_name in ("novel_analysis", "screenplay"):
        op.drop_table(table_name)
    op.create_table(
        "novel_analysis",
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("source_signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generated_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "screenplay",
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("source_signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("own_signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generated_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )

    op.drop_index("idx_chapter_project_no", table_name="chapter")
    op.drop_table("chapter")
