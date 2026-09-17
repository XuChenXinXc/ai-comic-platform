"""按对白行约束动态音频当前版本。

Revision ID: 20260818_0017_dynamic_audio_current
Revises: 20260816_0016_dynamic_job_idempotency
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0017_dynamic_audio_current"
down_revision: str | None = "20260816_0016_dynamic_job_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAME = "shot_audio"
_LEGACY_INDEX = "ux_shot_audio_current"
_CURRENT_INDEX = "ux_shot_audio_current_line"
_API_PROVIDER_BATCH_TABLE = "_alembic_tmp_api_provider"


def _has_table(table: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names()


def _has_index(table: str, index: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == index for item in inspector.get_indexes(table))


def _has_check_constraint(table: str, predicate_name: str) -> bool:
    """predicate_name: 子串片段，例如 'kind'，匹配 sqltext 包含该片段的 CHECK。"""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    needle = predicate_name.casefold()
    for item in inspector.get_check_constraints(table):
        sql = str(item.get("sqltext") or "").casefold()
        if needle in sql:
            return True
    return False


def _count_rows(table: str) -> int:
    if not _has_table(table):
        return 0
    result = op.get_bind().execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
    return int(result or 0)


def _verify_referenced_rows() -> None:
    """迁移前轻量预检：api_provider 引用行数与被引用次数应一致。

    说明：defer_foreign_keys 在本环境 alembic SQLite 适配下不生效；
    foreign_keys 已在 env.py 关闭。下面仅做防御性的行数比对，避免在
    数据被并行改动时出现 FK 引用缺口。

    只检查真实存在的引用列：历史上曾引用不存在的幽灵表
    （character_provider/image_provider/video_provider/tts_provider）与
    shot_audio.tts_provider_id（该列并不存在，SQLite 会把双引号未知列
    当作字符串字面量导致预检恒真），此处修正为实际 schema。
    """
    if not _has_table("api_provider"):
        return
    referenced_tables = [
        ("project", "default_text_provider_id"),
        ("project", "default_image_provider_id"),
        ("project", "default_video_provider_id"),
        ("asset_image", "provider_id"),
        ("keyframe_version", "provider_id"),
        ("video_version", "provider_id"),
        ("job", "provider_id"),
        ("audio_config", "tts_provider_id"),
        ("shot_audio", "provider_id"),
    ]
    connection = op.get_bind()
    provider_count = _count_rows("api_provider")
    refs_total = 0
    for table, column in referenced_tables:
        if not _has_table(table):
            continue
        rows = connection.execute(
            sa.text(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL')
        ).scalar()
        refs_total += int(rows or 0)
    if provider_count <= 0 and refs_total > 0:
        raise RuntimeError(
            "api_provider 行数与引用数不一致：providers=0 但 references="
            f"{refs_total}，请先修复数据再升级。"
        )


def _expand_api_provider_kind() -> None:
    if not _has_table("api_provider"):
        return
    inspector = sa.inspect(op.get_bind())
    constraints = inspector.get_check_constraints("api_provider")
    old_constraints = [
        str(item.get("name"))
        for item in constraints
        if item.get("name")
        and "kind" in str(item.get("sqltext") or "").casefold()
        and "tts" not in str(item.get("sqltext") or "").casefold()
    ]
    if not old_constraints:
        return

    check_sql = "kind IN ('text','image','video','tts')"
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        # 说明：defer_foreign_keys 在 alembic SQLite 适配下不生效；
        # env.py 已在连接级显式 PRAGMA foreign_keys=OFF 并 COMMIT，
        # 重建 api_provider 时不会触发 FK 检查。下面 batch_alter_table
        # 是 alembic 在 SQLite 上最稳的"删除原表+建新表"封装。
        # 临时表残留清理：迁移在创建临时表后中断时，下一次启动必须先清理。
        if _has_table(_API_PROVIDER_BATCH_TABLE):
            op.execute(sa.text(f'DROP TABLE "{_API_PROVIDER_BATCH_TABLE}"'))
        with op.batch_alter_table("api_provider", recreate="always") as batch_op:
            for name in old_constraints:
                batch_op.drop_constraint(op.f(name), type_="check")
            batch_op.create_check_constraint(op.f("ck_api_provider_kind"), check_sql)
        return

    for name in old_constraints:
        op.drop_constraint(name, "api_provider", type_="check")
    op.create_check_constraint("ck_api_provider_kind", "api_provider", check_sql)


def _collapse_api_provider_kind() -> None:
    """downgrade 时的 api_provider CHECK 回退。

    安全策略：tts 行存在时拒绝降级（避免把 tts 静默回退为 text 造成
    语义丢失）；tts 行为 0 时，把扩展后的 CHECK 还原为 legacy 三元组。
    """
    if not _has_table("api_provider"):
        return
    connection = op.get_bind()
    tts_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM api_provider WHERE kind = 'tts'")
        ).scalar()
        or 0
    )
    if tts_count > 0:
        raise RuntimeError(
            "downgrade refused: tts provider rows exist, "
            "migrate or delete them first"
        )

    # tts 行为 0：拆掉扩展后的 CHECK，保留原 legacy 三元组。
    legacy_sql = "kind IN ('text','image','video')"
    inspector = sa.inspect(connection)
    constraints = inspector.get_check_constraints("api_provider")
    drop_targets = [
        str(item.get("name"))
        for item in constraints
        if item.get("name")
        and "kind" in str(item.get("sqltext") or "").casefold()
        and "tts" in str(item.get("sqltext") or "").casefold()
    ]
    if not drop_targets:
        return

    dialect = connection.dialect.name
    if dialect == "sqlite":
        if _has_table(_API_PROVIDER_BATCH_TABLE):
            op.execute(sa.text(f'DROP TABLE "{_API_PROVIDER_BATCH_TABLE}"'))
        with op.batch_alter_table("api_provider", recreate="always") as batch_op:
            for name in drop_targets:
                batch_op.drop_constraint(op.f(name), type_="check")
            batch_op.create_check_constraint(
                op.f("ck_api_provider_kind_legacy"), legacy_sql
            )
        return

    for name in drop_targets:
        op.drop_constraint(name, "api_provider", type_="check")
    op.create_check_constraint(
        "ck_api_provider_kind_legacy", "api_provider", legacy_sql
    )


def _collapse_current_audio_for_legacy_constraint() -> None:
    if not _has_table(_TABLE_NAME):
        return
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, shot_id, dialogue_line_id, version_no, created_at "
            "FROM shot_audio WHERE is_current = 1 "
            "ORDER BY shot_id, "
            "CASE WHEN dialogue_line_id IS NULL THEN 0 ELSE 1 END, "
            "version_no DESC, created_at DESC, id DESC"
        )
    ).mappings().all()
    kept_shots: set[str] = set()
    for row in rows:
        shot_id = str(row.get("shot_id") or "")
        audio_id = str(row.get("id") or "")
        if not shot_id or not audio_id:
            continue
        if shot_id not in kept_shots:
            # 旧版按 shot 只有一个 current；若存在 legacy NULL 对白行，优先保留
            # 它，否则保留版本号/创建时间/id 排序后的最新对白行。
            kept_shots.add(shot_id)
            continue
        connection.execute(
            sa.text("UPDATE shot_audio SET is_current = 0 WHERE id = :audio_id"),
            {"audio_id": audio_id},
        )


def _create_current_index(name: str, columns: list[str]) -> None:
    if not _has_table(_TABLE_NAME) or _has_index(_TABLE_NAME, name):
        return

    predicate = sa.text("is_current = 1")
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.create_index(
            name,
            _TABLE_NAME,
            columns,
            unique=True,
            sqlite_where=predicate,
        )
    elif dialect == "postgresql":
        op.create_index(
            name,
            _TABLE_NAME,
            columns,
            unique=True,
            postgresql_where=predicate,
        )
    else:
        raise NotImplementedError(
            f"{_TABLE_NAME} current partial index is not supported on {dialect}"
        )


def _drop_index(name: str) -> None:
    if _has_table(_TABLE_NAME) and _has_index(_TABLE_NAME, name):
        op.drop_index(name, table_name=_TABLE_NAME)


def upgrade() -> None:
    # 幂等守卫：当前索引已存在且 legacy 索引不存在 ⇒ 已跑过。
    if _has_index(_TABLE_NAME, _CURRENT_INDEX) and not _has_index(
        _TABLE_NAME, _LEGACY_INDEX
    ):
        return
    _verify_referenced_rows()
    _expand_api_provider_kind()
    _drop_index(_LEGACY_INDEX)
    _create_current_index(_CURRENT_INDEX, ["shot_id", "dialogue_line_id"])


def downgrade() -> None:
    # 幂等守卫：legacy 索引已存在且当前索引不存在 ⇒ 已回退过。
    if _has_index(_TABLE_NAME, _LEGACY_INDEX) and not _has_index(
        _TABLE_NAME, _CURRENT_INDEX
    ):
        return
    _drop_index(_CURRENT_INDEX)
    _collapse_current_audio_for_legacy_constraint()
    _create_current_index(_LEGACY_INDEX, ["shot_id"])
    _collapse_api_provider_kind()
