from __future__ import annotations

import base64
from typing import Any

from app.api.schemas import ShotCreate

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _shot_create(**values: Any) -> ShotCreate:
    values.setdefault("duration_sec", 6)
    return ShotCreate(**values)
