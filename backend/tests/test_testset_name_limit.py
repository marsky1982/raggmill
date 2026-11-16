"""测试集名称长度上限：主生成与局部补生成共用同一常量与校验。

锁定的结论：
1. 上限是 MAX_TESTSET_NAME_LEN（80），GenIn / RegenIn / ExternalGenIn 的 name 都走同一个 Annotated 类型；
2. 首尾空白不占额度（校验前 strip）；
3. 超限时接口返回 422 且 detail 里带中文可读提示，不是裸的英文 max_length 报错。
"""
from __future__ import annotations

import pytest


def _too_long() -> str:
    from app.routers.testset import MAX_TESTSET_NAME_LEN

    return "名" * (MAX_TESTSET_NAME_LEN + 1)


def _at_limit() -> str:
    from app.routers.testset import MAX_TESTSET_NAME_LEN

    return "名" * MAX_TESTSET_NAME_LEN


def test_name_limit_shared_by_both_models():
    """GenIn 与 RegenIn 的 name 是同一个上限：80 字通过，81 字抛中文 ValueError。"""
    from pydantic import ValidationError

    from app.routers.testset import GenIn, RegenIn

    base = {"llm_preset_id": "p-llm", "embedding_preset_id": "p-emb", "name": _at_limit()}
    assert GenIn(kg_id="kg-x", **base).name == _at_limit()
    assert RegenIn(**base).name == _at_limit()

    with pytest.raises(ValidationError, match="测试集名称最多 80 个字符"):
        GenIn(kg_id="kg-x", **{**base, "name": _too_long()})
    with pytest.raises(ValidationError, match="测试集名称最多 80 个字符"):
        RegenIn(**{**base, "name": _too_long()})


def test_name_is_stripped_before_length_check():
    """首尾空白先去掉再判长：80 字加两侧空格仍然合法。"""
    from app.routers.testset import GenIn

    name = f"  {_at_limit()}  "
    out = GenIn(kg_id="kg-x", llm_preset_id="p-llm", embedding_preset_id="p-emb", name=name)
    assert out.name == _at_limit()


def test_generate_returns_readable_422_for_long_name():
    """主生成：超长名称在进入业务逻辑前被拦下，错误信息可读。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/testset/generate",
            json={
                "kg_id": "kg-not-exist", "llm_preset_id": "p-llm",
                "embedding_preset_id": "p-emb", "name": _too_long(),
            },
        )
    assert resp.status_code == 422
    assert "测试集名称最多 80 个字符" in str(resp.json()["detail"])


def test_regenerate_returns_readable_422_for_long_name():
    """局部补生成：同一个校验、同样的中文提示（校验先于「测试集不存在」的业务 404）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/testset/ts-not-exist/regenerate",
            json={"llm_preset_id": "p-llm", "embedding_preset_id": "p-emb", "name": _too_long()},
        )
    assert resp.status_code == 422
    assert "测试集名称最多 80 个字符" in str(resp.json()["detail"])


def test_external_import_shares_the_same_name_limit():
    """外部导入（generate-external）的 name 也走同一上限，之前是裸 str、任意长度都能落盘。"""
    from pydantic import ValidationError

    from app.routers.testset import ExternalGenIn

    base = {"mode": "chunks", "llm_preset_id": "p-llm", "embedding_preset_id": "p-emb"}
    assert ExternalGenIn(**base, name=f" {_at_limit()} ").name == _at_limit()
    with pytest.raises(ValidationError, match="测试集名称最多 80 个字符"):
        ExternalGenIn(**base, name=_too_long())
