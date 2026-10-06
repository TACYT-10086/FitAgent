"""grade_documents 与 search_policy 的测试。

LLM 一律打桩（真打要花钱、结果还不确定）；Milvus/Ollama 走真实调用，
与 test_rag.py 的风格一致。
"""

import pytest

from rag import retriever
from rag.retriever import grade_documents
from rag.transform import available_strategies
from tools import search_policy as sp

DOCS = [
    {
        "id": 1,
        "title": "场馆营业时间",
        "content": "周一至周五 06:00-23:00，周六周日 08:00-22:00。",
        "category": "场馆",
        "source": "场馆手册",
        "score": 0.92,
    },
    {
        "id": 2,
        "title": "停车与交通指引",
        "content": "地下停车场凭签到记录免费停车 3 小时。",
        "category": "场馆",
        "source": "到店指引",
        "score": 0.41,
    },
]


def _stub_grades(monkeypatch, payload):
    """把 chat_json 换成固定返回，并把收到的 messages 记下来备查。"""
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs.get("messages") or args[0])
        return payload

    monkeypatch.setattr(retriever, "chat_json", fake)
    return calls


# --------------------------------------------------------------- 基本判定
def test_grade_splits_relevant_and_irrelevant(monkeypatch):
    _stub_grades(
        monkeypatch,
        {"grades": [{"index": 0, "relevant": True}, {"index": 1, "relevant": False}]},
    )

    result = grade_documents("场馆几点开门", DOCS)

    assert result["grades"] == [True, False]
    assert [d["id"] for d in result["relevant"]] == [1]
    assert [d["id"] for d in result["irrelevant"]] == [2]
    assert result["sufficient"] is True


def test_grade_prompt_contains_question_and_doc_titles(monkeypatch):
    calls = _stub_grades(monkeypatch, {"grades": []})

    grade_documents("场馆几点开门", DOCS)

    joined = " ".join(m["content"] for m in calls[0])
    assert "场馆几点开门" in joined
    assert "场馆营业时间" in joined
    assert "停车与交通指引" in joined


# --------------------------------------------------------------- 对齐与容错
def test_grade_maps_by_index_not_by_response_order(monkeypatch):
    """模型乱序返回时，必须按 index 回填，不能按返回顺序。"""
    _stub_grades(
        monkeypatch,
        {
            "grades": [
                {"index": 1, "relevant": True},
                {"index": 0, "relevant": False},
            ]
        },
    )

    result = grade_documents("q", DOCS)

    assert result["grades"] == [False, True]


def test_grade_fails_open_on_missing_index(monkeypatch):
    """单条判定缺项就触发重试不划算，缺的按相关处理。"""
    _stub_grades(monkeypatch, {"grades": [{"index": 0, "relevant": False}]})

    result = grade_documents("q", DOCS)

    assert result["grades"] == [False, True]


@pytest.mark.parametrize(
    "payload",
    [
        {"grades": [{"index": 9, "relevant": True}]},      # 越界
        {"grades": [{"index": -1, "relevant": True}]},     # 负数
        {"grades": [123, "x", None]},                      # 不是对象
        {"grades": "nope"},                                # 类型都不对
        {},                                                # 缺键
    ],
)
def test_grade_survives_malformed_payload(monkeypatch, payload):
    _stub_grades(monkeypatch, payload)

    result = grade_documents("q", DOCS)

    assert result["grades"] == [True, True]  # 全部缺失 -> fail-open


def test_grade_treats_truthy_non_bool_as_relevant(monkeypatch):
    _stub_grades(
        monkeypatch,
        {"grades": [{"index": 0, "relevant": 1}, {"index": 1, "relevant": 0}]},
    )

    result = grade_documents("q", DOCS)

    assert result["grades"] == [True, False]


# --------------------------------------------------------------- sufficient
@pytest.mark.parametrize(
    "threshold,expected",
    [(1, True), (2, False), (3, False)],
)
def test_grade_sufficient_respects_threshold(monkeypatch, threshold, expected):
    _stub_grades(
        monkeypatch,
        {"grades": [{"index": 0, "relevant": True}, {"index": 1, "relevant": False}]},
    )

    assert grade_documents("q", DOCS, threshold=threshold)["sufficient"] is expected


# --------------------------------------------------------------- 空输入短路
def test_grade_empty_docs_short_circuits_without_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("docs 为空时不该调用 LLM")

    monkeypatch.setattr(retriever, "chat_json", boom)

    result = grade_documents("q", [])

    assert result["grades"] == []
    assert result["relevant"] == []
    assert result["irrelevant"] == []
    assert result["sufficient"] is False


# =============================================================== search_policy
# 循环逻辑全部用打桩的 retrieve_many / grade_documents / chat_json 驱动：
# 这里验的是「选策略 -> 检索 -> 评分 -> 换策略重试」的控制流，
# 真实向量检索路径由 test_rag.py 覆盖，两边职责不重叠、也互不拖累。
def _doc(doc_id: int, score: float) -> dict:
    return {
        "id": doc_id,
        "title": f"文档{doc_id}",
        "content": "正文",
        "category": "场馆",
        "source": "场馆手册",
        "score": score,
    }


def _graded(docs, *, relevant: bool) -> dict:
    return {
        "relevant": list(docs) if relevant else [],
        "irrelevant": [] if relevant else list(docs),
        "grades": [relevant] * len(docs),
        "sufficient": relevant,
        "reason": "",
    }


@pytest.fixture
def fakes(monkeypatch):
    """把 search_policy 的外部依赖全打桩，返回调用记录。"""

    def _install(*, grade_plan=(), default_relevant=True, hits=None, strategy_reply="rewrite"):
        record = {"retrieve": [], "grade": [], "select": []}
        plan = list(grade_plan)
        docs = hits if hits is not None else [_doc(1, 0.9), _doc(2, 0.5)]

        def fake_retrieve_many(queries, top_k=3):
            record["retrieve"].append(list(queries))
            return [list(docs) for _ in queries]

        def fake_grade(question, grade_docs, **kwargs):
            record["grade"].append(list(grade_docs))
            if plan:
                return plan.pop(0)
            return _graded(grade_docs, relevant=default_relevant)

        def fake_chat_json(messages, **kwargs):
            record["select"].append(messages)
            return {"strategy": strategy_reply}

        monkeypatch.setattr(sp, "retrieve_many", fake_retrieve_many)
        monkeypatch.setattr(sp, "grade_documents", fake_grade)
        monkeypatch.setattr(sp, "chat_json", fake_chat_json)
        return record

    return _install


# --------------------------------------------------------------- 权限门
@pytest.mark.parametrize("role", ["guest", "", None, "ADMIN", "root"])
def test_rejects_unauthorized_role_before_doing_any_work(monkeypatch, role):
    """权限校验必须是第一步：没过门就既不许检索、也不许调用 LLM。"""

    def boom(*args, **kwargs):
        raise AssertionError("权限没过就不该有任何检索 / LLM 调用")

    monkeypatch.setattr(sp, "retrieve_many", boom)
    monkeypatch.setattr(sp, "grade_documents", boom)
    monkeypatch.setattr(sp, "chat_json", boom)

    with pytest.raises(PermissionError):
        sp.search_policy("场馆几点开门", role)


@pytest.mark.parametrize("role", ["member", "coach", "admin"])
def test_allows_known_roles(fakes, role):
    fakes(grade_plan=[_graded([], relevant=True)])

    result = sp.search_policy("场馆几点开门", role)

    assert result["role"] == role
    assert result["sufficient"] is True


def test_result_has_expected_shape(fakes):
    fakes(grade_plan=[_graded([], relevant=True)])

    result = sp.search_policy("场馆几点开门", "member")

    assert set(result) >= {"question", "role", "strategy_used", "sufficient", "docs", "rounds"}
    assert set(result["rounds"][0]) >= {"strategy", "queries", "docs", "grade"}


# --------------------------------------------------------------- 轮次控制
def test_single_round_when_grade_suffices(fakes):
    record = fakes(grade_plan=[_graded([], relevant=True)])

    result = sp.search_policy("场馆几点开门", "member")

    assert len(result["rounds"]) == 1
    assert result["strategy_used"] == result["rounds"][0]["strategy"]
    assert len(record["grade"]) == 1


def test_retries_with_a_different_strategy_when_insufficient(fakes):
    fakes(
        grade_plan=[_graded([], relevant=False), _graded([], relevant=True)],
        strategy_reply="rewrite",
    )

    result = sp.search_policy("场馆几点开门", "member")

    assert len(result["rounds"]) == 2
    assert result["rounds"][0]["strategy"] == "rewrite"
    assert result["rounds"][1]["strategy"] != "rewrite"
    assert result["strategy_used"] == result["rounds"][1]["strategy"]
    assert result["sufficient"] is True


def test_stops_after_two_rounds_max(fakes):
    """最多 2 轮：评分一直不相关也不能无限重试。"""
    fakes(default_relevant=False)

    result = sp.search_policy("场馆几点开门", "member")

    assert len(result["rounds"]) == 2
    assert result["sufficient"] is False


def test_picks_better_round_when_both_insufficient(fakes):
    """两轮都不相关时，返回相关文档更多的那一轮（这里是第 1 轮）。"""
    better = {"relevant": [_doc(1, 0.9)], "irrelevant": [], "grades": [True], "sufficient": False, "reason": ""}
    worse = {"relevant": [], "irrelevant": [_doc(2, 0.5)], "grades": [False], "sufficient": False, "reason": ""}
    fakes(grade_plan=[better, worse], strategy_reply="rewrite")

    result = sp.search_policy("场馆几点开门", "member")

    assert result["sufficient"] is False
    assert result["strategy_used"] == result["rounds"][0]["strategy"]


def test_selects_strategy_via_llm_on_first_round_only(fakes):
    record = fakes(
        grade_plan=[_graded([], relevant=False), _graded([], relevant=True)],
        strategy_reply="hyde",
    )

    sp.search_policy("我怀孕了还能来健身吗", "member")

    # 第 2 轮换策略是确定性的，不该再问一次模型
    assert len(record["select"]) == 1
    assert len(record["retrieve"]) == 2


def test_explicit_strategy_skips_selection_call(fakes):
    record = fakes(grade_plan=[_graded([], relevant=True)])

    result = sp.search_policy("场馆几点开门", "member", strategy="hyde")

    assert record["select"] == []
    assert result["rounds"][0]["strategy"] == "hyde"


def test_unknown_selected_strategy_falls_back_to_default(fakes):
    fakes(grade_plan=[_graded([], relevant=True)], strategy_reply="瞎写的策略")

    result = sp.search_policy("场馆几点开门", "member")

    assert result["rounds"][0]["strategy"] == "rewrite"


def test_results_truncated_to_top_k(fakes):
    fakes(
        grade_plan=[_graded([], relevant=True)],
        hits=[_doc(i, 1.0 - i / 10) for i in range(1, 6)],
    )

    result = sp.search_policy("场馆几点开门", "member", top_k=2)

    assert len(result["docs"]) == 2


# --------------------------------------------------------------- 纯函数
def test_fuse_dedups_by_id_and_keeps_highest_score():
    fused = sp._fuse(
        [
            [{"id": 1, "score": 0.5}, {"id": 2, "score": 0.8}],
            [{"id": 1, "score": 0.9}],
        ]
    )

    assert [d["id"] for d in fused] == [1, 2]
    assert fused[0]["score"] == 0.9


def test_fuse_empty_input():
    assert sp._fuse([]) == []


@pytest.mark.parametrize("current", ["rewrite", "multi_query", "hyde"])
def test_different_strategy_never_returns_input(current):
    assert sp._different_strategy(current) != current


def test_different_strategy_normalizes_input():
    assert sp._different_strategy("HyDE") != "hyde"


def test_different_strategy_cycles_through_registry():
    names = available_strategies()
    current = names[0]
    seen = [current]
    for _ in range(len(names) - 1):
        current = sp._different_strategy(current)
        seen.append(current)

    # 走满一圈恰好把其余策略各过一遍，不重复、不遗漏
    assert seen[1:] == names[1:]
    assert len(set(seen)) == len(names)


# --------------------------------------------------------------- 真打 DeepSeek（默认跳过）
@pytest.mark.llm
def test_llm_grade_documents_judges_real_pairs():
    relevant_doc = {
        "id": 1,
        "title": "场馆营业时间",
        "content": "本馆营业时间为周一至周五 06:00-23:00，周六周日 08:00-22:00。",
        "category": "场馆",
        "source": "场馆手册",
        "score": 0.9,
    }
    unrelated_doc = {
        "id": 2,
        "title": "储物柜租用规则",
        "content": "临时储物柜免费使用，需自备挂锁；长期储物柜按月 100 元租用。",
        "category": "场馆",
        "source": "场馆手册",
        "score": 0.9,
    }

    assert grade_documents("场馆几点开门，晚上几点关？", [relevant_doc])["sufficient"] is True
    assert grade_documents("场馆几点开门，晚上几点关？", [unrelated_doc])["sufficient"] is False


@pytest.mark.llm
def test_llm_search_policy_end_to_end():
    result = sp.search_policy("会员卡有几种，各自多少钱？", "member")

    assert set(result) >= {"question", "role", "strategy_used", "sufficient", "docs", "rounds"}
    assert result["rounds"][0]["strategy"] in available_strategies()
    assert 1 <= len(result["rounds"]) <= 2
