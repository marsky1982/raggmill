"""Step 2 公共工具：题型比例与条数分配、出题语言、节点小工具。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations


SYNTHESIZER_MAP = {
    "single_hop_specific": "SingleHopSpecificQuery",
    "multi_hop_abstract": "MultiHopAbstractQuery",
    "multi_hop_specific": "MultiHopSpecificQuery",
}


def calculate_split_values(probs: list[float], size: int) -> list[int]:
    """按比例把 size 条精确分给各题型（最大余数法，合计恰为 size）。

    这是**实际生成**的分配口径，不是对 ragas 的近似：本项目自己编排出题阶段
    （见 _PhasedGeneration），按这里的结果给每个合成器传 n，并把场景截到 n 条。
    刻意不沿用 ragas 自带 synthesizers.utils.calculate_split_values 的 ceil 逐类向上取整：
    它的合计会超出 size（10 条按 .5/.25/.25 → 5/3/3=11；1 条按 .7/.3/0 → 2 条），多花 token。
    前端显示、成本预估、实际出题三处共用本函数，保证一致。probs 须已归一化。
    """
    values = [int(size * p) for p in probs]
    remainder = size - sum(values)
    fracs = sorted(range(len(probs)), key=lambda i: size * probs[i] - values[i], reverse=True)
    for i in range(remainder):
        values[fracs[i % len(fracs)]] += 1
    return values


def default_distribution() -> dict:
    return {"single_hop_specific": 0.5, "multi_hop_abstract": 0.25, "multi_hop_specific": 0.25}


DIST_KEYS = ["single_hop_specific", "multi_hop_abstract", "multi_hop_specific"]


def normalize_distribution(dist: dict) -> tuple[list[float], float]:
    """题型比例归一化：返回 (按 DIST_KEYS 顺序、合计为 1 的比例, 原始合计)。

    前端可能提交合计 ≠ 1 的比例（如三项都填 1）。ragas 按 size×p 直接分配条数，
    不归一化就会生成 3 倍条数、烧 3 倍 token。
    """
    raw = []
    for k in DIST_KEYS:
        try:
            p = float(dist.get(k, 0) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"题型比例 {k} 不是数字：{dist.get(k)!r}")
        if p < 0 or p != p:  # 负数 / NaN
            raise ValueError(f"题型比例 {k} 不能为负数或 NaN：{p}")
        raw.append(p)
    total = sum(raw)
    if total <= 0:
        raise ValueError("query 分布比例之和必须大于 0")
    return [p / total for p in raw], total


# 出题语言选项：值 -> (中文label, 注入 ragas llm_context 的指令)
# 指令必须用英文写 —— ragas 的 prompt 主体是英文，同语种指令更容易被模型遵守。
LANGUAGE_OPTIONS: dict[str, tuple[str, str]] = {
    "": ("跟随语料（不指定）", ""),
    "zh": (
        "中文",
        "IMPORTANT: Write BOTH the question and the answer in Simplified Chinese (简体中文). "
        "Keep proper nouns, product names and technical terms in their original form as they "
        "appear in the context. Never answer in English.",
    ),
    "en": (
        "英文",
        "IMPORTANT: Write BOTH the question and the answer in English. "
        "Translate content from the context into English where necessary, "
        "but do not invent facts that are not in the context.",
    ),
}


def _language_directive(language: str) -> str | None:
    """把语言选项转成 ragas llm_context 文本；未指定时返回 None（保持 ragas 原行为）。"""
    return LANGUAGE_OPTIONS.get(language or "", ("", ""))[1] or None


def _node_kind(node) -> str:
    return getattr(node.type, "name", str(node.type)).upper()


def _has_content(value) -> bool:
    """属性存在且有内容；空列表 / 空字符串不等同于「有素材」。"""
    if value is None:
        return False
    if isinstance(value, (str, list, tuple, set, dict)):
        return len(value) > 0
    return True
