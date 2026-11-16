"""方案 A 的护栏：后端推送的英文 stage 字面量必须都能在前端映射表里找到。

前端 `frontend/src/lib/stageLabels.ts` 是阶段名的唯一译名表（查不到原样显示）。
本测试扫描 `backend/app` 里的 stage 字面量，防止后端新增阶段名而忘补译名。

例外（不要求进表）：
- 建图阶段名：后端 `kg.py` 的 `STAGE_DESC` 直接推中文标题；
- 评估：`metric:{key}` 是动态前缀，前端按前缀统一翻译；
- 其它非 ASCII 取值（演示模式 / 建图中文字段）同样视为后端已中文化。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend" / "app"
LABELS_TS = ROOT / "frontend" / "src" / "lib" / "stageLabels.ts"

_STAGE_PATTERNS = (
    re.compile(r"""["']stage["']\s*:\s*["']([^"']+)["']"""),
    re.compile(r"""\bset_stage\(\s*["']([^"']+)["']"""),
    re.compile(r"""\bStage\(\s*["']([^"']+)["']"""),
)


def _frontend_keys() -> set[str]:
    text = LABELS_TS.read_text(encoding="utf-8")
    block = text.split("Object.freeze({", 1)[1].split("})", 1)[0]
    return set(re.findall(r"^\s{2}([A-Za-z_]\w*):", block, flags=re.M))


def test_backend_stage_literals_are_covered_by_frontend_labels():
    keys = _frontend_keys()
    assert keys, "没能从 stageLabels.ts 解析出任何译名"

    missing: dict[str, str] = {}
    seen: set[str] = set()
    for path in sorted(BACKEND.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in _STAGE_PATTERNS:
                for value in pattern.findall(line):
                    seen.add(value)
                    if not value.isascii():
                        continue  # 建图 / 演示阶段名后端已直接给中文
                    if value.startswith("metric:"):
                        continue  # 动态前缀，前端按前缀翻译
                    if value not in keys:
                        missing[f"{path.relative_to(ROOT)}:{lineno}"] = value

    # 防止扫描规则失效后测试空转
    assert {"fetching", "samples", "projection"} <= seen

    assert not missing, (
        "以下后端阶段名在前端 stageLabels.ts 里没有译名（新增阶段请补表）：\n"
        + "\n".join(f"  {where} -> {value}" for where, value in missing.items())
    )
