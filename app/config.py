"""运行期配置。所有配置均通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件路径；默认放在 data/ 下，Compose 会把该目录挂载为卷
DB_PATH = Path(os.getenv("DB_PATH", "/data/museum.db"))


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


# 初始化失败时是否直接退出（容器场景应快速失败）
DB_EAGER_INIT = _parse_bool(os.getenv("DB_EAGER_INIT", "true"))

# 浮点比较用的极小容差，避免累计误差导致边界判断错误
EPSILON = 1e-9
