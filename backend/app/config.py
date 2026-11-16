"""路径与运行环境配置。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 项目根 = backend/ 的上一级
ROOT_DIR = Path(__file__).resolve().parents[2]
# 运行时数据目录；可用环境变量 RAGAS_STUDIO_DATA_DIR 覆盖（测试用 tmp 目录隔离真实 data/）
DATA_DIR = Path(os.environ.get("RAGAS_STUDIO_DATA_DIR") or ROOT_DIR / "data").resolve()
PRESETS_DIR = DATA_DIR / "presets"
UPLOADS_DIR = DATA_DIR / "uploads"
KG_DIR = DATA_DIR / "kg"
TESTSET_DIR = DATA_DIR / "testset"
EXPORT_DIR = TESTSET_DIR / "exports"
RAGFLOW_DIR = DATA_DIR / "ragflow"
EVALUATION_DIR = DATA_DIR / "evaluation"
SECRET_KEY_FILE = DATA_DIR / "secret.key"
# 破坏性维护动作（如重新绑定数据源）的备份根目录：生产落在仓库根 `.backups/`（已 gitignore），
# 测试随 DATA_DIR 落到临时目录旁，不污染仓库
BACKUP_DIR = DATA_DIR.parent / ".backups"


def ensure_data_dirs() -> None:
    """创建运行时目录。由 main.py 的 lifespan 调用——import 本模块不应触碰文件系统。"""
    for d in (DATA_DIR, PRESETS_DIR, UPLOADS_DIR, KG_DIR, TESTSET_DIR, EXPORT_DIR, RAGFLOW_DIR, EVALUATION_DIR):
        d.mkdir(parents=True, exist_ok=True)


# 演示模式开关：环境变量 RAGAS_DEMO=1 强制 mock；默认自动探测
FORCE_DEMO = os.environ.get("RAGAS_DEMO", "") == "1"

# 关闭 ragas 的用量统计上报（必须在 import ragas 之前）：ragas 在每次 LLM 调用里**同步**
# requests.post 到它的统计服务器（llms/base.py → _analytics.track），网络不通时 SSL 握手
# 会卡住出题线程的事件循环，任务永远停在 running；本地优先工具也不该往外发用量事件。
# 用户显式设了 RAGAS_DO_NOT_TRACK 时尊重用户的值。
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")


def _try_import_ragas() -> bool:
    try:
        import ragas  # noqa: F401
        return True
    except Exception:
        return False


RAGAS_AVAILABLE = False if FORCE_DEMO else _try_import_ragas()
MODE = "ragas" if RAGAS_AVAILABLE else "demo"
