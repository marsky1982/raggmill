"""所有生成入口共享的执行参数；拒绝拼错或未实现的成本字段。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class GenerationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_retries: int = Field(default=3, ge=1, le=10)
    llm_timeout: int = Field(default=180, ge=1, le=1800)
    max_workers: int = Field(default=8, ge=1, le=32)
    token_budget: int = Field(default=0, ge=0)
    num_personas: int = Field(default=3, ge=1, le=10)
    language: Literal["", "zh", "en"] = ""

    def execution_kwargs(self) -> dict:
        return {key: getattr(self, key) for key in GenerationPolicy.model_fields}
