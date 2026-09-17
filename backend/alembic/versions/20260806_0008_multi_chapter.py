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

⚠️ 警告：此迁移会清空历史数据！
按用户要求不迁移历史数据：正文、分析、剧本、分镜及其媒体版本会被清空，
素材库（asset / asset_image）与项目配置保留。

在执行前，请确认：
1. 已备份数据库
2. 已导出重要数据（如有需要）
3. 了解此操作不可逆

如需保留数据，请在升级前设置环境变量：
  export SKIP_DATA_DELETION=1

历史备注：本迁移是「迁移只增量」约定的显式例外（用户当时的明确决策）。
迁移内的导出目录基于进程 CWD 相对路径 data_export/，且含 time.sleep(3)
等待导出落盘；新迁移不得复制此模式。
"""

import json
import os
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0008"
down_revision: str | None = "20260805_0007"
branch_labels: str | None = None
depends_on: str | None = None


def export_data_before_deletion() -> None:
    """在删除数据前导出备份"""
    bind = op.get_bind()
    export_dir = Path("data_export") / (
        f"migration_{revision}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )

    tables_to_export = ["shot", "novel_analysis", "screenplay", "job"]
    existing_tables = set(sa.inspect(bind).get_table_names())

    print(f"\n{'='*60}")
    print(f"数据导出到: {export_dir.absolute()}")
    print(f"{'='*60}\n")

    failed_tables: list[str] = []
    for table_name in tables_to_export:
        if table_name not in existing_tables:
            continue

        try:
            result = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
            if result == 0:
                print(f"  - {table_name}: 0 条记录（跳过）")
                continue

            rows = bind.execute(sa.text(f"SELECT * FROM {table_name}")).mappings().fetchall()
            # 目录延迟到确实有数据要写时才建，否则空库/测试反复跑迁移会堆积空目录。
            export_dir.mkdir(parents=True, exist_ok=True)
            export_file = export_dir / f"{table_name}.json"
            with open(export_file, "w", encoding="utf-8") as f:
                json.dump([dict(row) for row in rows], f, ensure_ascii=False, indent=2)
            print(f"  ✓ {table_name}: {len(rows)} 条记录已导出")
        except Exception as e:
            print(f"  ✗ {table_name}: 导出失败 - {e}")
            failed_tables.append(f"{table_name}: {e}")

    if failed_tables:
        raise RuntimeError(
            f"迁移前数据备份失败，已终止迁移以防止数据丢失：{', '.join(failed_tables)}"
        )

    if not export_dir.exists():
        print("  （无数据需要导出，未创建导出目录）")

    print(f"\n{'='*60}\n")


def upgrade() -> None:
    # 检查是否跳过数据删除
    skip_deletion = os.getenv("SKIP_DATA_DELETION", "").lower() in ("1", "true", "yes")

    if not skip_deletion:
        print("\n" + "="*60)
        print("⚠️  警告：此迁移将删除以下数据：")
        print("  - 所有分镜 (shot)")
        print("  - 所有关键帧和视频版本 (keyframe_version, video_version)")
        print("  - 所有剧本分析 (novel_analysis)")
        print("  - 所有剧本 (screenplay)")
        print("  - 所有任务记录 (job)")
        print("\n素材库和项目配置将被保留。")
        print("="*60)

        # 导出数据
        export_data_before_deletion()

        # 再次确认
        print("\n如需取消，请立即终止此进程（Ctrl+C）")
        print("如需跳过删除，请设置环境变量: SKIP_DATA_DELETION=1\n")
        import time
        time.sleep(3)

    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

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

    # 删除数据（如果未跳过）
    if not skip_deletion:
        for table_name in (
            "keyframe_version",
            "video_version",
            "shot",
            "novel_analysis",
            "screenplay",
            "job",
        ):
            if table_name in existing_tables:
                print(f"  删除 {table_name} 中的所有数据...")
                op.execute(f"DELETE FROM {table_name}")
    else:
        print("\n⚠️  跳过数据删除（SKIP_DATA_DELETION=1）")
        print("注意：这可能导致数据不一致，请手动处理数据迁移。\n")
        # 逃生舱补强：跳过删除时也必须先把 novel_analysis/screenplay 导出
        # 落盘，否则下面重建表会直接丢数据；导出目录见 export_data_before_deletion。
        export_data_before_deletion()

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
            # 逃生舱（跳过删除）时存量 shot 行没有 chapter 归属：加列为可空，
            # 否则 SQLite 重建表时 NOT NULL 无默认值必然失败；NULL 行对
            # 新查询不可见（按 chapter_id 过滤），由用户手动迁移补齐。
            batch.add_column(
                sa.Column(
                    "chapter_id",
                    sa.Text(),
                    sa.ForeignKey("chapter.id", ondelete="CASCADE"),
                    nullable=skip_deletion,
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
