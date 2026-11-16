"""Step 2 导出：CSV / JSONL（字段与 ragas from_list 兼容，CSV 防公式注入）。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

import csv
import io
import json


# ---------- 导出 ----------

# CSV 单元格以这些字符开头时，Excel / WPS 会当作公式执行（CSV 注入，）
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_CSV_FORMULA_PREFIXES) else text


def _csv_list(items) -> str:
    """列表型字段写成 JSON 数组字符串：原文含 `||` 也能无损还原（旧格式用 ` || ` 拼接，）。"""
    return json.dumps(list(items or []), ensure_ascii=False)


def export_testset(ts_data: dict, fmt: str, scope: str, types: list[str] | None) -> tuple[str, bytes]:
    samples = ts_data["samples"]
    if scope == "adopted":
        samples = [s for s in samples if s.get("adopted")]
    # 题型筛选与范围叠加：界面在「全部 / 已采纳」下选了题型时也要生效
    if types:
        samples = [s for s in samples if s.get("synthesizer_name") in types]
    if not samples:
        raise ValueError("所选范围内没有样本可导出")

    ts_id, name = ts_data["id"], ts_data["name"]
    # 演示数据必须能和真实数据区分：文件名后缀 + 每行带 is_mock
    is_mock = bool(ts_data.get("is_mock")) or any(s.get("is_mock") for s in samples)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        header = ["user_input", "reference", "reference_contexts",
                  "retrieved_contexts", "response", "synthesizer_name",
                  "query_style", "query_length", "persona_name"]
        if is_mock:
            header.append("is_mock")
        w.writerow(header)
        for s in samples:
            row = [_csv_cell(s["user_input"]), _csv_cell(s["reference"]),
                   _csv_cell(_csv_list(s["reference_contexts"])),
                   _csv_cell(_csv_list(s.get("retrieved_contexts"))),
                   _csv_cell(s.get("response", "")), s["synthesizer_name"],
                   s.get("query_style", ""), s.get("query_length", ""),
                   _csv_cell(s.get("persona_name", ""))]
            if is_mock:
                row.append("true")
            w.writerow(row)
        content = buf.getvalue().encode("utf-8-sig")
    elif fmt == "jsonl":
        lines = []
        for s in samples:
            row = {
                "user_input": s["user_input"], "reference": s["reference"],
                "reference_contexts": s["reference_contexts"],
                "synthesizer_name": s["synthesizer_name"],
                "query_style": s.get("query_style", ""),
                "query_length": s.get("query_length", ""),
                "persona_name": s.get("persona_name", ""),
            }
            # 空的 response / retrieved_contexts 不写：写成 "" / [] 会让 ragas 评测误以为这两列已存在
            if s.get("response"):
                row["response"] = s["response"]
            if s.get("retrieved_contexts"):
                row["retrieved_contexts"] = s["retrieved_contexts"]
            if is_mock:
                row["is_mock"] = True
            lines.append(json.dumps(row, ensure_ascii=False))
        content = ("\n".join(lines) + "\n").encode("utf-8")
    else:
        raise ValueError(f"不支持的格式: {fmt}（仅 csv / jsonl）")

    suffix = "-演示数据" if is_mock else ""
    filename = f"{name}-{scope}{suffix}.{fmt}"
    return filename, content
