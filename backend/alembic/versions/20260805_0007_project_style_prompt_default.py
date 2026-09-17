"""project style prompt default

Revision ID: 20260805_0007
Revises: 20260803_0006
Create Date: 2026-08-05

移除画风自动识别能力后，画风只允许用户手动填写。
这里把默认值写入表默认约束，并回填历史项目中仍为空的画风。
"""

from alembic import op
from sqlalchemy import inspect

revision: str = "20260805_0007"
down_revision: str | None = "20260803_0006"
branch_labels: str | None = None
depends_on: str | None = None

DEFAULT_STYLE_PROMPT = (
    "电影感古风仙域二维国漫，稳定自然的半写实人物比例，东方审美骨相，"
    "克制细腻的古风线条，低饱和青灰、玉白、玄黑与暖金综合色调，"
    "仙气通透但不过度梦幻，真实清晰的微表情，层次分明的电影光影，"
    "东方山水空间美学，精致但不浮夸。"
)


def _has_style_prompt() -> bool:
    columns = inspect(op.get_bind()).get_columns("project")
    return any(column["name"] == "style_prompt" for column in columns)


def upgrade() -> None:
    # 从更早版本链式升级时 project.style_prompt 可能尚未创建，此时跳过
    if not _has_style_prompt():
        return
    with op.batch_alter_table("project") as batch:
        batch.alter_column("style_prompt", server_default=DEFAULT_STYLE_PROMPT)
    # 只回填空画风，不覆盖用户已经手动填写的内容
    op.execute(
        "UPDATE project SET style_prompt = '"
        + DEFAULT_STYLE_PROMPT.replace("'", "''")
        + "' WHERE style_prompt IS NULL OR trim(style_prompt) = ''"
    )


def downgrade() -> None:
    if not _has_style_prompt():
        return
    with op.batch_alter_table("project") as batch:
        batch.alter_column("style_prompt", server_default="")
