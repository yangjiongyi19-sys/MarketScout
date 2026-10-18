import pytest

from app.domain.learning import digest
from app.infrastructure.capability_registry import validate
from scripts.learning import compile_strategy_skill


def candidate(status="captured"):
    draft = {
        "title": "保留最新购物约束",
        "applies_when": "买家在多轮选购中更新了硬性筛选条件。",
        "steps": ["按当前轮次重建查询条件", "用商品搜索工具重新核验"],
        "exceptions": ["买家撤回条件时不得继续沿用"],
        "evidence_refs": ["fixture:constraint-update"],
        "keywords": ["多轮选购", "筛选条件"],
        "scope": "shopping",
    }
    base = {}
    return {
        "id": "a" * 64,
        "status": status,
        "document": draft,
        "base": base,
        "content_hash": digest({"draft": draft, "base": base}),
    }


def test_strategy_candidate_compiles_to_validated_evaluation_skill():
    document = compile_strategy_skill(candidate())

    assert validate(document) == document
    assert document["kind"] == "skill"
    assert document["allowed_tools"] == ["product_search_tool"]
    assert "按当前轮次重建查询条件" in document["body"]
    assert "买家撤回条件时不得继续沿用" in document["body"]
    assert document["evidence"][0]["reference"].split(":")[1] == "a" * 64


@pytest.mark.parametrize("status", ["rejected", "withdrawn", "published"])
def test_non_candidate_status_cannot_be_compiled(status):
    with pytest.raises(ValueError, match="不可用"):
        compile_strategy_skill(candidate(status))


def test_tampered_candidate_is_rejected():
    row = candidate()
    row["document"]["steps"][0] = "绕过审核"

    with pytest.raises(ValueError, match="哈希"):
        compile_strategy_skill(row)
