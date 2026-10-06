"""eval 指标计算的单测：不跑检索、不打 LLM。"""

import pytest

from eval.rag_eval import hit_at_k, recall_at_k, reciprocal_rank


# --------------------------------------------------------------- hit@k（准确率）
def test_hit_when_any_ground_truth_returned():
    assert hit_at_k(["停车与交通指引", "场馆营业时间"], ("场馆营业时间",)) == 1


def test_hit_any_of_multiple_ground_truths():
    assert hit_at_k(["B", "x"], ("A", "B")) == 1


def test_miss_when_nothing_matches():
    assert hit_at_k(["停车与交通指引"], ("场馆营业时间",)) == 0


def test_hit_is_order_independent():
    assert hit_at_k(["x", "场馆营业时间", "y"], ("场馆营业时间",)) == 1


def test_miss_on_empty_returns():
    assert hit_at_k([], ("场馆营业时间",)) == 0


# --------------------------------------------------------------- recall@k
def test_recall_partial_coverage():
    assert recall_at_k(["A"], ("A", "B")) == pytest.approx(0.5)


def test_recall_full_coverage():
    assert recall_at_k(["A", "B"], ("A", "B")) == pytest.approx(1.0)


def test_recall_zero_when_nothing_matches():
    assert recall_at_k(["x"], ("A",)) == pytest.approx(0.0)


def test_recall_ignores_duplicate_returns():
    """同一篇文档被多路召回重复返回，不该把召回率刷上去。"""
    assert recall_at_k(["A", "A", "A"], ("A", "B")) == pytest.approx(0.5)


def test_recall_empty_returns():
    assert recall_at_k([], ("A",)) == pytest.approx(0.0)


def test_recall_empty_ground_truth_does_not_divide_by_zero():
    assert recall_at_k(["A"], ()) == pytest.approx(0.0)


# --------------------------------------------------------------- MRR@k
def test_mrr_is_one_when_ground_truth_is_first():
    assert reciprocal_rank(["A", "x", "y"], ("A",), k=3) == pytest.approx(1.0)


def test_mrr_is_half_when_ground_truth_is_second():
    """这正是 hit@1 丢掉、而 MRR 保留的信息：doc 从第 1 位掉到第 2 位。"""
    assert reciprocal_rank(["x", "A", "y"], ("A",), k=3) == pytest.approx(0.5)


def test_mrr_uses_first_match_only():
    assert reciprocal_rank(["x", "A", "B"], ("A", "B"), k=3) == pytest.approx(0.5)


def test_mrr_zero_when_outside_cutoff():
    assert reciprocal_rank(["x", "y", "A"], ("A",), k=2) == pytest.approx(0.0)


def test_mrr_zero_when_absent():
    assert reciprocal_rank(["x", "y"], ("A",), k=3) == pytest.approx(0.0)


def test_mrr_depth_comes_from_uncut_candidates():
    """候选比 k 深是 MRR 能算出真实名次的前提。"""
    assert reciprocal_rank(["x", "A", "y"], ("A",), k=3) == pytest.approx(0.5)
    assert reciprocal_rank(["x", "A", "y"], ("A",), k=1) == pytest.approx(0.0)
