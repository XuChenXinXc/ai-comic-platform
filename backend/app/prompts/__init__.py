from pathlib import Path

_PROMPT_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """读取内置提示词模板。"""
    path = _PROMPT_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()


__all__ = ["load_prompt"]
