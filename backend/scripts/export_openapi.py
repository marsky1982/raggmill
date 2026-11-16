"""离线导出 OpenAPI JSON（不启动服务、不需要端口）。

用途：前端 `npm run gen:api` 与 CI 一致性检查共用同一份规范来源。
前置条件：只需 backend 的 venv，不连数据库/外部服务；不读写 data/。

用法（仓库根目录）：
  backend/.venv/Scripts/python.exe backend/scripts/export_openapi.py
  backend/.venv/Scripts/python.exe backend/scripts/export_openapi.py --out frontend/openapi.json

退出码：0 成功；2 参数/环境问题；1 生成失败。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_OUT = BACKEND_DIR / "openapi.json"


def build_spec() -> dict:
    from app.main import app

    return app.openapi()


def dump_spec(spec: dict) -> str:
    """稳定序列化：固定缩进 + 保留中文 + 结尾换行，便于 git diff 与 CI 比对。"""
    return json.dumps(spec, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 OpenAPI JSON")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出文件路径")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if not out.is_absolute():
        out = Path.cwd() / out
    try:
        spec = build_spec()
        text = dump_spec(spec)
    except Exception as exc:  # 生成失败说明应用装配有问题，交给调用方判断
        print(f"OpenAPI 生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text.encode("utf-8"))
    print(f"OpenAPI 已写入 {out}（paths={len(spec['paths'])}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
