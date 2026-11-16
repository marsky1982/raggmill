"""条数分配：前端显示（/api/calc/splits）、成本预估、实际生成三处共用 calculate_split_values。

口径（见 设计文档）：比例先归一化，再用最大余数法分配，**合计恰为 testset_size**。
本项目自己编排出题阶段，按这里的结果给每个合成器传 n 并把场景截到 n，所以它就是实际生成口径。
刻意不跟 ragas 自带 synthesizers.utils.calculate_split_values 的 math.ceil（合计会超出 size，多花 token）。
"""
from __future__ import annotations

import pytest

CASES = [
    ([0.5, 0.25, 0.25], 10),
    ([0.5, 0.25, 0.25], 50),
    ([1 / 3, 1 / 3, 1 / 3], 10),
    ([0.6, 0.2, 0.2], 7),
    ([1.0, 0.0, 0.0], 5),
    ([0.0, 0.5, 0.5], 9),
    ([0.7, 0.3, 0.0], 1),
    ([0.5, 0.25, 0.25], 0),
]


@pytest.mark.parametrize("probs,size", CASES)
def test_split_values_sum_equals_size(probs: list[float], size: int):
    from app.ragas_adapter.testset import calculate_split_values

    values = calculate_split_values(probs, size)
    assert sum(values) == size
    assert all(v >= 0 for v in values)
    # 比例为 0 的题型分不到条数（否则会被提交，多跳抽象题 find_n_indirect_clusters(0) 报错）
    assert all(v == 0 for p, v in zip(probs, values) if p == 0)


@pytest.mark.parametrize("dist,size", [
    ({"single_hop_specific": 1, "multi_hop_abstract": 1, "multi_hop_specific": 1}, 6),
    ({"single_hop_specific": 2, "multi_hop_abstract": 1, "multi_hop_specific": 1}, 10),
])
def test_unnormalized_distribution_sum_equals_size(dist: dict, size: int):
    """前端可能提交合计 ≠ 1 的比例：归一化后合计仍恰为 size（不会翻倍生成）。"""
    from app.ragas_adapter.testset import calculate_split_values, normalize_distribution

    probs, _ = normalize_distribution(dist)
    assert sum(calculate_split_values(probs, size)) == size
