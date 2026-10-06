"""朴素 RAG vs Agentic RAG 的对比评测。

用法：
    python -m eval.rag_eval                      # 完整跑（含让模型自选策略）
    python -m eval.rag_eval --no-strategy-llm    # 固定 rewrite，省调用、可复现
    python -m eval.rag_eval --top-k 3            # 放宽指标口径（默认 1）

产出 eval/rag_eval.json，并在终端打印对比表格。

为什么检索深度与指标口径分开：
    两种模式都按 PROBE_K 拉同样深的一批候选，再按 --top-k 切出指标口径。
    只按 top-k 检索的话，排在前 k 名之外的排名信息全丢了，
    MRR 就只能知道「没进前 k」，算不出真实的倒数排名。
    —— 20 条语料 + top_k=3 时 hit@k 几乎恒为 1，正是踩了这个坑才没有区分度。

指标口径（本步不生成最终答案，只评检索结果）：
    hit@k      准确率：top-k 里命中任一 ground-truth 即记 1
    recall@k   召回率：被覆盖的 ground-truth 占比
    MRR@k      第一个命中的 ground-truth 的倒数排名，对排序退化敏感
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import get_env
from eval.queries import EVAL_QUERIES
from llm import DEFAULT_MODEL
from rag.retriever import retrieve
from tools.search_policy import search_policy

# 指标口径：默认只看第 1 名。20 条语料下 top_k=3 的 hit@k 没有区分度。
TOP_K = 1
# 检索深度：拉这么深才够算 MRR（排在前 k 名之外的也要看得到）
PROBE_K = 10

EVAL_ROLE = "member"
OUTPUT_PATH = Path(__file__).resolve().parent / "rag_eval.json"


def hit_at_k(returned_titles: list[str], ground_truth: tuple[str, ...]) -> int:
    """query 级准确率：命中任一 ground-truth 记 1，否则 0。"""
    return 1 if set(returned_titles) & set(ground_truth) else 0


def recall_at_k(returned_titles: list[str], ground_truth: tuple[str, ...]) -> float:
    """召回率：覆盖到的 ground-truth 比例。用集合去重，重复返回刷不了分。"""
    expected = set(ground_truth)
    if not expected:
        return 0.0
    return len(set(returned_titles) & expected) / len(expected)


def reciprocal_rank(
    returned_titles: list[str], ground_truth: tuple[str, ...], k: int
) -> float:
    """第一个命中的 ground-truth 的倒数排名；前 k 名内没有则 0。

    对排序退化敏感：doc 从第 1 位掉到第 2 位，hit@1 会从 1 掉到 0，
    而 MRR 从 1.0 掉到 0.5 —— 两者一起看才能区分「没检索到」和「排序变差了」。
    """
    expected = set(ground_truth)
    for rank, title in enumerate(returned_titles[:k], start=1):
        if title in expected:
            return 1.0 / rank
    return 0.0


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def run_naive(question: str, probe_k: int = PROBE_K) -> dict:
    """基线：原问题不加工，直接检索。"""
    started = time.perf_counter()
    docs = retrieve(question, top_k=probe_k)

    return {"titles": [d["title"] for d in docs], "latency_ms": _elapsed_ms(started)}


def run_agentic(
    question: str, *, probe_k: int = PROBE_K, strategy: str | None = None
) -> dict:
    """Agentic：选策略 -> 检索 -> 评分 -> 必要时换策略重试。"""
    started = time.perf_counter()
    result = search_policy(question, EVAL_ROLE, top_k=probe_k, strategy=strategy)

    return {
        "strategy": result["strategy_used"],
        "rounds": len(result["rounds"]),
        "sufficient": result["sufficient"],
        "titles": [d["title"] for d in result["docs"]],
        "latency_ms": _elapsed_ms(started),
    }


def _first_rank(returned_titles: list[str], ground_truth: tuple[str, ...]) -> int | None:
    """ground-truth 在候选里的首个名次（1 起）；没出现则 None。仅用于人工排查。"""
    expected = set(ground_truth)
    for rank, title in enumerate(returned_titles, start=1):
        if title in expected:
            return rank
    return None


def _summarize(rows: list[dict], mode: str) -> dict:
    n = len(rows)
    if not n:
        return {
            "accuracy": 0.0,
            "recall": 0.0,
            "mrr": 0.0,
            "hits": 0,
            "n": 0,
            "mean_latency_ms": 0.0,
        }

    hits = sum(row[mode]["hit"] for row in rows)
    return {
        "accuracy": round(hits / n, 4),
        "recall": round(sum(row[mode]["recall"] for row in rows) / n, 4),
        "mrr": round(sum(row[mode]["mrr"] for row in rows) / n, 4),
        "hits": hits,
        "n": n,
        "mean_latency_ms": round(sum(row[mode]["latency_ms"] for row in rows) / n, 1),
    }


def evaluate(
    *, top_k: int = TOP_K, probe_k: int = PROBE_K, strategy: str | None = None
) -> dict:
    rows = []
    for item in EVAL_QUERIES:
        naive = run_naive(item.question, probe_k)
        agentic = run_agentic(item.question, probe_k=probe_k, strategy=strategy)

        for mode, run in (("naive", naive), ("agentic", agentic)):
            # 指标只看前 top_k 名；MRR 也用同一口径截断，两模式才可比
            run["hit"] = hit_at_k(run["titles"][:top_k], item.ground_truth)
            run["recall"] = round(recall_at_k(run["titles"][:top_k], item.ground_truth), 4)
            run["mrr"] = round(reciprocal_rank(run["titles"], item.ground_truth, top_k), 4)
            # 候选深度内真实的排名，方便排查「是没检索到还是排太后」
            run["ground_truth_rank"] = _first_rank(run["titles"], item.ground_truth)

        rows.append(
            {
                "qid": item.qid,
                "question": item.question,
                "ground_truth": list(item.ground_truth),
                "hard_for_naive": item.hard_for_naive,
                "note": item.note,
                "naive": naive,
                "agentic": agentic,
            }
        )

    naive_summary = _summarize(rows, "naive")
    agentic_summary = _summarize(rows, "agentic")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "top_k": top_k,
            "probe_k": probe_k,
            "model": DEFAULT_MODEL,
            "num_queries": len(EVAL_QUERIES),
            "db_name": get_env("DB_NAME", ""),
            "collection": get_env("COLLECTION_NAME", ""),
            "strategy_selection": "llm" if strategy is None else f"fixed:{strategy}",
        },
        "summary": {
            "naive": naive_summary,
            "agentic": agentic_summary,
            "delta": {
                key: round(agentic_summary[key] - naive_summary[key], 4)
                for key in ("accuracy", "recall", "mrr")
            },
        },
        "per_query": rows,
    }


def _print_table(report: dict) -> None:
    summary = report["summary"]
    naive, agentic, delta = summary["naive"], summary["agentic"], summary["delta"]
    top_k = report["config"]["top_k"]

    print("=" * 86)
    print(f"指标口径 top_k={top_k}（候选深度 {report['config']['probe_k']}）")
    print("-" * 86)
    print(f"{'模式':<10}{'准确率 hit@k':>14}{'召回率':>12}{'MRR@k':>10}{'命中':>8}{'平均耗时(ms)':>14}")
    print("-" * 86)
    for name, row in (("朴素 RAG", naive), ("Agentic", agentic)):
        print(
            f"{name:<10}{row['accuracy']:>14.2%}{row['recall']:>12.2%}{row['mrr']:>10.3f}"
            f"{row['hits']:>6}/{row['n']:<2}{row['mean_latency_ms']:>12.1f}"
        )
    print("-" * 86)
    print(
        f"{'提升':<10}{delta['accuracy']:>+14.2%}{delta['recall']:>+12.2%}{delta['mrr']:>+10.3f}"
    )
    print("=" * 86)

    print(f"\n逐题明细（命中=前 {top_k} 名；排名=候选深度内首个命中的名次）：")
    for row in report["per_query"]:
        mark_naive = "✓" if row["naive"]["hit"] else "✗"
        mark_agentic = "✓" if row["agentic"]["hit"] else "✗"
        hard = " [难]" if row["hard_for_naive"] else ""
        rank_n = row["naive"]["ground_truth_rank"] or "-"
        rank_a = row["agentic"]["ground_truth_rank"] or "-"
        print(
            f"  {row['qid']} {mark_naive}朴素(#{rank_n}) / {mark_agentic}Agentic(#{rank_a})"
            f"  策略={row['agentic']['strategy']}({row['agentic']['rounds']}轮){hard}"
            f"  {row['question']}"
        )


def main() -> None:
    # Windows 控制台默认 cp936，直接 print 中文表格会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="朴素 RAG vs Agentic RAG 对比评测")
    parser.add_argument(
        "--top-k", type=int, default=TOP_K, help=f"指标口径取前几名，默认 {TOP_K}"
    )
    parser.add_argument(
        "--probe-k",
        type=int,
        default=PROBE_K,
        help=f"检索深度（须 >= top-k），默认 {PROBE_K}；调小可省 token",
    )
    parser.add_argument(
        "--no-strategy-llm",
        action="store_true",
        help="固定用 rewrite 策略，跳过「让模型选策略」的调用（省 token、结果可复现）",
    )
    args = parser.parse_args()

    if args.top_k > args.probe_k:
        parser.error(f"--top-k({args.top_k}) 不能大于 --probe-k({args.probe_k})")

    strategy = "rewrite" if args.no_strategy_llm else None
    report = evaluate(top_k=args.top_k, probe_k=args.probe_k, strategy=strategy)

    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _print_table(report)
    print(f"\n已写出 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
