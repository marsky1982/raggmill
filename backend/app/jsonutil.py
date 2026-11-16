"""JSON 安全化：把非有限浮点（NaN / ±Infinity）换成 null。

来源：ragas 的指标在「无法判定」时会把 np.nan 当哨兵值返回，例如 faithfulness
生成 0 条陈述（`metrics/_faithfulness.py`）、context_precision 的候选为空、
context_recall 分母为 0、answer_relevancy 的 LLM 没给出 question、
answer_correctness 的提示词返回空。`json.dumps` 默认把 NaN 写成 `NaN` 字面量
（不是合法 JSON），FastAPI 的 JSON 响应则直接拒绝序列化，于是整张列表 500。
"""
from __future__ import annotations

import math
import numbers
from typing import Any


def sanitize_nonfinite(value: Any) -> Any:
    """递归复制并清洗容器里的 NaN / ±Infinity；其余值原样返回。"""
    if isinstance(value, dict):
        return {key: sanitize_nonfinite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_nonfinite(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return value
    if isinstance(value, numbers.Real):
        # numpy.float64/float32 也走这里；统一转 Python float，保证可 json 序列化
        number = float(value)
        return number if math.isfinite(number) else None
    return value
