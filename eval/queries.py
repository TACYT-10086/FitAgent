"""对比评测用的 10 条问题 + ground-truth 标题。

ground_truth 必须与 db/seed.py::KNOWLEDGE_DOCS 里的标题**逐字一致** ——
差一个字就匹配不上，指标会恒为 0，而且很难看出是标注错了还是检索错了。

hard_for_naive 标的是「朴素 RAG 大概率漏检」的题：这些题问法与语料用词差异大
（口语化、换了说法），正是查询改写该发挥作用的场景。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalQuery:
    qid: str
    question: str
    ground_truth: tuple[str, ...]
    hard_for_naive: bool = False
    note: str = ""


EVAL_QUERIES: tuple[EvalQuery, ...] = (
    EvalQuery(
        "q01",
        "场馆几点开门，晚上几点关？",
        ("场馆营业时间",),
        note="问法与语料用词接近，作为简单对照",
    ),
    EvalQuery(
        "q02",
        "会员卡有几种，各自多少钱？",
        ("会员卡类型与价格",),
        note="「几种/多少钱」需要映射到「类型/价格」",
    ),
    EvalQuery(
        "q03",
        "私教课 10 节要多少钱？",
        ("私教课收费标准",),
        note="具体节数只在正文里出现，标题不含",
    ),
    EvalQuery(
        "q04",
        "更衣室和淋浴间免费吗？",
        ("更衣室与淋浴间",),
        note="问法接近语料标题",
    ),
    EvalQuery(
        "q05",
        "开车来停车怎么收费？",
        ("停车与交通指引",),
        note="「收费」vs「免费 3 小时」",
    ),
    EvalQuery(
        "q06",
        "我要去外地出差两个月，会员卡能先停着不算时间吗？",
        ("会员卡冻结规则",),
        hard_for_naive=True,
        note="「停着/出差」vs「冻结/伤病」，口语化且无共同关键词",
    ),
    EvalQuery(
        "q07",
        "卡不想练了，剩下的钱能退吗？",
        ("退卡与退款政策",),
        hard_for_naive=True,
        note="与「私教课可以退款吗」竞争，需要按会籍卡语义区分",
    ),
    EvalQuery(
        "q08",
        "我朋友能拿我的卡来锻炼吗？",
        ("会员卡能否转让",),
        hard_for_naive=True,
        note="「拿我的卡来锻炼」vs「转让/实名」，零词汇重合",
    ),
    EvalQuery(
        "q09",
        "我怀孕了还能来健身吗？",
        ("特殊人群锻炼须知",),
        hard_for_naive=True,
        note="「怀孕」与「特殊人群」无字面重合，纯靠语义",
    ),
    EvalQuery(
        "q10",
        "私教课买了一年没上完会作废吗？",
        ("私教课有效期",),
        hard_for_naive=True,
        note="「作废」vs「有效期/延长」",
    ),
)
