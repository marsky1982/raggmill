"""Step 2：测试集生成 + 导出（真实 ragas / 演示 mock）——对外门面。

实现按职责拆在同目录的 testset_*.py 子模块里（工程化遗留：单文件曾超过 2,500 行）。
这里原样再导出全部旧名字（含测试用到的私有名字），路由、脚本与测试的导入路径不变。

⚠ monkeypatch 要打在**真正调用该名字的子模块**上（如 testset_generation.build_llm），
打在本门面上不会影响子模块内部的调用。
"""
from __future__ import annotations

from .testset_common import (  # noqa: F401
    SYNTHESIZER_MAP,
    calculate_split_values,
    default_distribution,
    DIST_KEYS,
    normalize_distribution,
    LANGUAGE_OPTIONS,
    _language_directive,
    _node_kind,
    _has_content,
)
from .testset_demo import (  # noqa: F401
    _QUERIES,
    _ANSWERS,
    _generate_demo_samples,
)
from .testset_synth import (  # noqa: F401
    _term_key,
    _canonical_term,
    _align_multihop_concepts,
    _build_persona_safe_synthesizer,
    _PhasedGeneration,
    _persona_name_key,
    _match_persona,
    _normalize_persona_mapping,
)
from .testset_capabilities import (  # noqa: F401
    CAPABILITY_LABELS,
    _ENHANCEMENT_MODES,
    _similarity_gap_message,
    _kg_flags,
    _synth_material_ok,
    _recommend_relation_property,
    assess_kg_capabilities,
    check_generation_capabilities,
    _precheck_kg,
    _enhancement_needs,
    _unfillable_gaps,
)
from .testset_enhance import (  # noqa: F401
    _enhancement_usage,
    EnhancementStopped,
    _material_counts,
    _enhance_projection_kg,
)
from .testset_generation import (  # noqa: F401
    _model_meta,
    _ragflow_generation_fingerprint,
    generate_testset,
    _STOP_LABELS,
    _delivery_gap,
    _generate_ragas_samples,
)
from .testset_external import (  # noqa: F401
    prepare_external_kg_from_json,
    prepare_external_kg_from_chunks,
    generate_testset_external,
)
from .testset_export import (  # noqa: F401
    _CSV_FORMULA_PREFIXES,
    _csv_cell,
    _csv_list,
    export_testset,
)
from .testset_cost import (  # noqa: F401
    _CALIB,
    _CALIB_CHUNK_CHARS,
    _CALIB_ENHANCE,
    _CALIB_ENHANCE_MODEL,
    _CALIB_EMBED_TOKENS_PER_NODE,
    _estimate_enhancement,
    _abstract_context_expansion,
    estimate_generation_cost,
)
from .testset_health import (  # noqa: F401
    _PROP_LABELS,
    _EDGE_LABELS,
    kg_health_report,
    _prop_explain,
)
