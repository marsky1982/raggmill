"""计算辅助：比例 -> 每类实际条数（与实际出题、成本预估同一口径，见 testset.calculate_split_values）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..ragas_adapter.testset import SYNTHESIZER_MAP, calculate_split_values, normalize_distribution

router = APIRouter(prefix="/api/calc", tags=["calc"])

KEYS = ["single_hop_specific", "multi_hop_abstract", "multi_hop_specific"]


class SplitsIn(BaseModel):
    testset_size: int
    distribution: dict


@router.post("/splits")
def splits(inp: SplitsIn):
    try:
        probs, raw_total = normalize_distribution(inp.distribution)
    except ValueError as e:
        raise HTTPException(400, str(e))
    normalized = abs(raw_total - 1.0) > 1e-6
    values = calculate_split_values(probs, inp.testset_size)
    return {
        "normalized": normalized,
        "splits": {SYNTHESIZER_MAP[k]: v for k, v in zip(KEYS, values)},
        "sum": sum(values),
    }
