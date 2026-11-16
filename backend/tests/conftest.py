"""pytest 公共夹具。

铁律：测试绝不能碰仓库里的真实 data/。
app.config 在 import 时根据环境变量 RAGAS_STUDIO_DATA_DIR 决定 DATA_DIR，
所以这里必须在任何 `import app...` 之前把它指向临时目录——
测试模块请在函数/夹具内部再 import app，不要在模块顶层 import。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REAL_DATA_DIR = BACKEND_DIR.parent / "data"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(scope="session", autouse=True)
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """整个测试会话共用的 DATA_DIR（tmp 下，初始不存在，由被测代码自行创建）。"""
    d = tmp_path_factory.mktemp("session") / "data"
    os.environ["RAGAS_STUDIO_DATA_DIR"] = str(d)
    # 防呆：若 app.config 已被提前 import（某测试模块顶层 import 了 app），DATA_DIR 就固定成别的值了
    if "app.config" in sys.modules:
        from app import config

        assert config.DATA_DIR == d.resolve(), (
            f"app.config 在 conftest 设置环境变量前就被 import 了，DATA_DIR={config.DATA_DIR}"
        )
    yield d
    from app import config

    assert REAL_DATA_DIR.resolve() not in config.DATA_DIR.resolve().parents
    assert config.DATA_DIR.resolve() != REAL_DATA_DIR.resolve()
