"""Step 2 演示模式：RAGAS_AVAILABLE=False 时的 mock 样本（落盘打 is_mock）。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

import asyncio

from ..tasks.manager import Task, manager
from .testset_common import SYNTHESIZER_MAP


_QUERIES = {
    "SingleHopSpecificQuery": [
        "这个工具支持哪些文档格式上传？",
        "知识图谱保存后如何复用？",
        "导出 JSONL 后如何读回？",
        "单跳具体型 query 适合评估什么能力？",
        "上传文档的最小 token 要求是多少？",
    ],
    "MultiHopAbstractQuery": [
        "综合多个文档，总结该产品的整体定位与设计理念",
        "对比不同部署方案的成本与适用场景",
        "该产品在安全与可观测性方面有哪些贯穿性的设计？",
        "从架构演进的角度评价两步工作流的取舍",
    ],
    "MultiHopSpecificQuery": [
        "结合计费策略与性能指标两节，估算月度成本",
        "对比接口协议与部署架构，说明扩容路径",
        "故障处理流程与数据治理策略如何衔接？",
        "安全合规范式在前后端各落地了哪些具体措施？",
    ],
}


_ANSWERS = {
    "SingleHopSpecificQuery": "根据文档第 {n} 节：支持 .md / .html / .docx 三种格式，PDF 需预先解析后喂入。知识图谱生成后以 ragas 原生 JSON 落盘，Step 2 可直接选择复用，无需重算。",
    "MultiHopAbstractQuery": "综合多节内容：产品定位为「ragas 是引擎、工具是驾驶舱」，以两步工作流解耦知识图谱构建与测试集生成，强调可视化、可复用与本地优先。",
    "MultiHopSpecificQuery": "跨节推导：结合计费（按 token 用量统计）与性能（长任务异步 + SSE 流式）章节，月度成本主要由 LLM 调用次数与并发配置决定，建议通过 KG 复用降低重复计算。",
}


async def _generate_demo_samples(task: Task, kg_entry: dict, splits: list[int], keys: list[str]) -> list[dict]:
    """演示模式：模板数组取问题 + .format() 填答案。

    ⚠ MOCK 数据：仅在 RAGAS_AVAILABLE=False（ragas 未安装）时作为兜底。
    问题来自硬编码模板数组 _QUERIES，答案用 .format() 填占位符，
    与真实 KG 内容无关，不能用于评测。请安装 ragas 后走 _generate_ragas_samples。
    """
    # ---- 醒目告警：当前为 mock 数据，不能用于真实评测 ----
    manager.log(
        task,
        "⚠ 警告：当前为 MOCK 演示数据（ragas 未安装 / RAGAS_AVAILABLE=False）。\n"
        "  • 问题来自硬编码模板数组，与 KG 内容无关\n"
        "  • 答案用 .format() 填占位符，非 LLM 生成\n"
        "  • reference_contexts 是假的 '[KG chunk-x-y]' 字符串\n"
        "  • 此测试集不能用于 RAG 评测，仅供 UI / 落盘 / 导出链路验证\n"
        "  • 解决：pip install ragas（或确认 .venv 内 ragas 可导入）后重新生成",
        extra={"stage": "warn_mock", "progress": 0.05, "level": "warn", "is_mock": True},
    )
    for i in range(1, 4):
        if task.cancel_event.is_set():
            raise asyncio.CancelledError
        manager.log(task, f"[MOCK] generate_personas / scenarios 阶段 {i}/3（模拟）",
                    extra={"stage": "scenarios", "progress": 0.1 + i * 0.1, "is_mock": True})
        await asyncio.sleep(0.6)

    samples: list[dict] = []
    total = sum(splits) or 1
    idx = 0
    for k, cnt in zip(keys, splits):
        sname = SYNTHESIZER_MAP[k]
        for j in range(cnt):
            if task.cancel_event.is_set():
                raise asyncio.CancelledError
            q = _QUERIES[sname][j % len(_QUERIES[sname])]
            samples.append({
                "index": idx,
                "synthesizer_name": sname,
                "user_input": q,
                "reference": _ANSWERS[sname].format(n=j + 1),
                "reference_contexts": [f"[MOCK chunk-{idx}-{j}] 《{kg_entry['name']}》相关片段……"],
                "retrieved_contexts": [],
                "response": "",
                "adopted": True,
                "is_mock": True,  # 标记为 mock 数据，前端可识别并显示告警
            })
            idx += 1
            await asyncio.sleep(0.15)
            manager.log(task, f"[MOCK] sample {idx}/{total}: {sname}",
                        extra={"stage": "samples", "progress": 0.4 + 0.6 * idx / total, "is_mock": True})
    # 收尾告警
    manager.log(
        task,
        f"⚠ MOCK 测试集生成完毕（{idx} 条）。此数据不可用于评测，请安装 ragas 后重新生成。",
        extra={"stage": "warn_mock_done", "progress": 0.4, "level": "warn", "is_mock": True},
    )
    return samples
