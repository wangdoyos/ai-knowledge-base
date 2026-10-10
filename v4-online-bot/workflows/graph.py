"""LangGraph 工作流组装：规划 -> 采集 -> 分析 -> 审核 -> 整理入库。

7 个智能体节点各自独立成模块，接入 :class:`~langgraph.graph.StateGraph`：

- Planner（workflows/planner.py）：动态规划策略；
- Collector（workflows/collector.py）：数据采集；
- Analyzer（workflows/analyzer.py）：LLM 单条分析；
- Reviewer（workflows/reviewer.py）：5 维加权审核；
- Reviser（workflows/reviser.py）：读反馈定向修改；
- Organizer（workflows/organizer.py）：整理入库（正常终点）；
- HumanFlag（workflows/human_flag.py）：人工介入（异常终点）。

拓扑如下::

    planner -> collector -> analyzer -> reviewer --(通过)--> organizer -> END
                                           ^ |
                                 reviser <-+ +--(超限)--> human_flag -> END

Planner 产出的 plan 驱动全链路：采集节点消费 per_source_limit，
整理节点消费 relevance_threshold，审核后的路由消费 max_iterations。
Reviewer 审核的对象是 analyses（articles 在 organizer 之后才存在），
审核后三路分支：

- 通过 -> organizer 整理入库；
- 未通过且未超限 -> reviser 按反馈定向改写后回审，形成循环；
- 未通过且达循环上限（plan 的 max_iterations 优先，默认
  MAX_REVIEW_ITERATIONS） -> human_flag 转人工介入，
  问题条目写入 knowledge/pending_review/，不进主知识库。

Example:
    >>> from workflows.graph import build_graph
    >>> app = build_graph()
    >>> final = app.invoke(make_initial_state())
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from workflows.analyzer import analyze_node  # noqa: E402
from workflows.collector import collect_node  # noqa: E402
from workflows.human_flag import human_flag_node  # noqa: E402
from workflows.model_client import (  # noqa: E402
    BudgetExceededError,
    get_cost_guard,
)
from workflows.organizer import organize_node  # noqa: E402
from workflows.planner import planner_node  # noqa: E402
from workflows.reviewer import review_node  # noqa: E402
from workflows.reviser import revise_node  # noqa: E402
from workflows.state import (  # noqa: E402
    KBState,
    MAX_REVIEW_ITERATIONS,
    make_initial_state,
)

logger = logging.getLogger(__name__)

# 条件边路由的三个分支键（与目标节点同名）
BRANCH_ORGANIZE = "organizer"
BRANCH_REVISE = "reviser"
BRANCH_HUMAN_FLAG = "human_flag"


def route_after_review(state: KBState) -> str:
    """审核后的三路分支路由：通过 / 返工改写 / 转人工介入。

    - 通过 -> :data:`BRANCH_ORGANIZE`；
    - 未通过且 iteration < 循环上限（plan 的 ``max_iterations`` 优先，
      默认 :data:`~workflows.state.MAX_REVIEW_ITERATIONS`）
      -> :data:`BRANCH_REVISE`，按反馈定向改写后回审；
    - 未通过且达上限 -> :data:`BRANCH_HUMAN_FLAG`，超限说明问题
      不在质量而在数据，交给人工判断，同时也切断了死循环。

    Args:
        state: 读取 ``review_passed`` / ``iteration`` / ``plan``。

    Returns:
        分支键：:data:`BRANCH_ORGANIZE` / :data:`BRANCH_REVISE` /
        :data:`BRANCH_HUMAN_FLAG`。
    """
    if state["review_passed"]:
        return BRANCH_ORGANIZE
    max_iterations = int(state["plan"].get("max_iterations", MAX_REVIEW_ITERATIONS))
    if state["iteration"] >= max_iterations:
        logger.warning(
            "审核循环达上限（%d/%d 轮）仍未通过，转人工介入",
            state["iteration"],
            max_iterations,
        )
        return BRANCH_HUMAN_FLAG
    logger.info("审核未通过（第 %d 轮），转改写节点返工", state["iteration"])
    return BRANCH_REVISE


def build_graph():
    """组装并编译知识库构建工作流。

    Returns:
        编译后的 LangGraph 应用（CompiledStateGraph），
        可直接 ``invoke`` / ``stream``。
    """
    graph = StateGraph(KBState)

    graph.add_node("planner", planner_node)
    graph.add_node("collector", collect_node)
    graph.add_node("analyzer", analyze_node)
    graph.add_node("reviewer", review_node)
    graph.add_node("reviser", revise_node)
    graph.add_node("organizer", organize_node)
    graph.add_node("human_flag", human_flag_node)

    # 线性主干：规划 -> 采集 -> 分析 -> 审核（审核对象是 analyses）
    graph.set_entry_point("planner")
    graph.add_edge("planner", "collector")
    graph.add_edge("collector", "analyzer")
    graph.add_edge("analyzer", "reviewer")

    # 三路条件分支：通过进整理入库，未通过转改写，超限转人工
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {
            BRANCH_ORGANIZE: "organizer",
            BRANCH_REVISE: "reviser",
            BRANCH_HUMAN_FLAG: "human_flag",
        },
    )
    # 改写后回审，形成 审核 <-> 改写 循环
    graph.add_edge("reviser", "reviewer")
    # 两个终点：整理入库（正常）与人工介入（异常）
    graph.add_edge("organizer", END)
    graph.add_edge("human_flag", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------


def _summarize_update(node: str, update: dict[str, Any]) -> str:
    """把单个节点的状态更新压缩成一行摘要，供流式打印。

    Args:
        node: 节点名。
        update: 该节点返回的部分状态更新。

    Returns:
        一行摘要文本。
    """
    parts: list[str] = []
    for field in ("sources", "analyses", "articles"):
        if field in update:
            parts.append(f"{field}={len(update[field])} 条")
    if "review_passed" in update:
        parts.append(f"passed={update['review_passed']}")
    if update.get("needs_human_review"):
        parts.append("needs_human_review=True")
    if "review_feedback" in update and update["review_feedback"]:
        parts.append(f"feedback={update['review_feedback'][:40]}…")
    if "iteration" in update:
        parts.append(f"iteration={update['iteration']}")
    if "cost_tracker" in update:
        tracker = update["cost_tracker"]
        parts.append(
            f"tokens={tracker['total_tokens']}（{tracker['calls']} 次调用）"
        )
    return "、".join(parts) or "（无状态更新）"


def _print_cost_report() -> None:
    """打印全局 CostGuard 的成本报告（按节点分组）并落盘 JSON。

    没有任何 LLM 调用时只打一行提示，不落盘空报告。
    """
    guard = get_cost_guard()
    report = guard.get_report()
    print(f"\n{'=' * 60}\n成本报告\n{'=' * 60}")
    if not report["total_calls"]:
        print("本次运行没有 LLM 调用，无成本产生")
        return
    print(
        f"总成本 {report['total_cost_yuan']:.6f} 元 / 预算 "
        f"{report['budget_yuan']:.2f} 元（已用 {report['usage_ratio']:.0%}）、"
        f"LLM 调用 {report['total_calls']} 次、"
        f"tokens={report['total_prompt_tokens']}+{report['total_completion_tokens']}"
    )
    by_node = sorted(
        report["by_node"].items(),
        key=lambda kv: kv[1]["cost_yuan"],
        reverse=True,
    )
    for node, stats in by_node:
        print(
            f"  - {node}：{stats['calls']} 次、"
            f"tokens={stats['prompt_tokens']}+{stats['completion_tokens']}、"
            f"成本 {stats['cost_yuan']:.6f} 元（占 {stats['cost_percent']:.1f}%）"
        )
    path = guard.save_report()
    print(f"报告已保存：{path}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = build_graph()
    print(f"{'=' * 60}\n知识库构建工作流启动\n{'=' * 60}")

    # stream_mode="updates"：每步产出 {节点名: 部分状态更新}
    final_state: dict[str, Any] = dict(make_initial_state())
    budget_exceeded = False
    try:
        for step in app.stream(make_initial_state(), stream_mode="updates"):
            for node, update in step.items():
                final_state.update(update or {})
                print(f"\n>>> 节点 [{node}] 完成：{_summarize_update(node, update or {})}")
    except BudgetExceededError as exc:
        budget_exceeded = True
        print(f"\n!!! 预算熔断，工作流提前中断：{exc}")

    tracker = final_state["cost_tracker"]
    if budget_exceeded:
        outcome = "预算熔断提前中断"
    elif final_state.get("needs_human_review"):
        outcome = "转人工介入（见 knowledge/pending_review/）"
    else:
        outcome = f"入库 {len(final_state['articles'])} 条"
    print(
        f"\n{'=' * 60}\n运行结束："
        f"{outcome}、"
        f"审核 {final_state['iteration']} 轮、"
        f"passed={final_state['review_passed']}、"
        f"tokens={tracker['total_tokens']}（LLM 调用 {tracker['calls']} 次）"
        f"\n{'=' * 60}"
    )

    # 收尾：无论正常结束还是预算熔断，都打印并落盘成本报告
    _print_cost_report()
