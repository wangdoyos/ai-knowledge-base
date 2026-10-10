"""Reviewer 智能体节点：对分析结果做 5 维度加权评分审核。

审核对象是 ``state["analyses"]``（分析节点的产出），而非 articles ——
articles 要到整理（organize）之后才存在。评分维度与权重：

- summary_quality（摘要质量）：25%
- technical_depth（技术深度）：25%
- relevance（相关性）：20%
- originality（原创性）：15%
- formatting（格式规范）：15%

每维 1~10 分，**加权总分由代码重算**（不信任模型算术），
总分 >= 7.0 视为通过。送审对象是与整理节点同口径的“达标候选”
（score >= plan.relevance_threshold），按分数降序取前 5 条控 token 消耗。

Example:
    >>> from workflows.reviewer import review_node
    >>> from workflows.state import make_initial_state
    >>> update = review_node(make_initial_state())
    >>> update["review_passed"]
    True
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from workflows.model_client import (  # noqa: E402
    LLMError,
    accumulate_usage,
    chat_json,
)
from workflows.state import KBState  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 各评分维度的权重，合计 1.0；加权总分由代码按此重算
REVIEW_WEIGHTS: dict[str, float] = {
    "summary_quality": 0.25,  # 摘要质量
    "technical_depth": 0.25,  # 技术深度
    "relevance": 0.20,        # 相关性
    "originality": 0.15,      # 原创性
    "formatting": 0.15,       # 格式规范
}

# 加权总分达到该阈值视为审核通过
PASS_THRESHOLD = 7.0
# 无显式计划时的送审门槛：与整理节点 organizer.SCORE_THRESHOLD 对齐
DEFAULT_RELEVANCE_THRESHOLD = 0.6
# 只送审前 N 条 analyses，控制单次审核的 token 消耗
MAX_REVIEW_ITEMS = 5
# 评分任务用低温度，保证多次运行评分一致
REVIEW_TEMPERATURE = 0.1

REVIEWER_SYSTEM_PROMPT = """你是一个知识库质量审核员。对给定的分析结果列表整体打分，
共 5 个维度，每个维度 1~10 分（整数或一位小数）：

- summary_quality：摘要质量（中文表达通顺、信息量充足、无空话）
- technical_depth：技术深度（key_points 是否触及实现原理与技术要点）
- relevance：相关性（内容与 AI/LLM/Agent 主题的贴合程度）
- originality：原创性（摘要是否为提炼总结，而非照抄原文简介）
- formatting：格式规范（字段完整、标签粒度合适、风格统一）

feedback 必须给出具体、可执行的修改建议（指明第几条、哪个字段、怎么改）；
若各维度都很好则 feedback 为空字符串。不需要计算总分。

返回 JSON，格式：
{
  "scores": {"summary_quality": 8, "technical_depth": 7,
             "relevance": 9, "originality": 7, "formatting": 8},
  "feedback": ""
}
只返回 JSON 对象，不要输出其他内容。"""


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _review_threshold(plan: dict[str, Any]) -> float:
    """审核送审门槛：与整理节点同口径，plan 优先，缺省 0.6。

    Args:
        plan: 任务计划，可能为空 dict。

    Returns:
        0~1 的相关性阈值。
    """
    try:
        return float(plan.get("relevance_threshold", DEFAULT_RELEVANCE_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_RELEVANCE_THRESHOLD


def _weighted_total(scores: dict[str, Any]) -> float:
    """用代码重算加权总分，不信任模型返回的算术结果。

    缺失或非法的维度按 0 分计；单维度得分被截断到 0~10 区间，
    避免模型返回越界数值抬高或压低总分。

    Args:
        scores: 模型返回的各维度得分字典。

    Returns:
        0~10 的加权总分。
    """
    total = 0.0
    for dim, weight in REVIEW_WEIGHTS.items():
        try:
            value = float(scores.get(dim, 0.0))
        except (TypeError, ValueError):
            logger.warning("维度 %s 的得分非法：%r，按 0 分计", dim, scores.get(dim))
            value = 0.0
        total += weight * min(max(value, 0.0), 10.0)
    return total


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


def review_node(state: KBState) -> dict[str, Any]:
    """Reviewer 节点：对 analyses 做 5 维度加权评分，决定通过或返工。

    加权总分由 :func:`_weighted_total` 在代码里重算，
    >= :data:`PASS_THRESHOLD` 为通过；只送审前
    :data:`MAX_REVIEW_ITEMS` 条 analyses 以控制 token 消耗。
    LLM 调用失败时自动通过（降级策略，不阻塞流程）。

    Args:
        state: 读取 ``plan`` / ``analyses`` / ``iteration`` / ``cost_tracker``。

    Returns:
        ``{"review_passed": bool, "review_feedback": str,
        "iteration": int, "cost_tracker": {...}}``。
    """
    analyses = state["analyses"]
    iteration = state["iteration"] + 1
    tracker = dict(state["cost_tracker"])
    print(f"[Review] 第 {iteration} 轮审核：共 {len(analyses)} 条分析结果")

    if not analyses:
        # 无内容可审：直接放行，让流程自然空转结束
        print("[Review] analyses 为空，跳过审核")
        return {
            "review_passed": True,
            "review_feedback": "",
            "iteration": iteration,
            "cost_tracker": tracker,
        }

    # 任务计划作为审核背景上下文；空 dict 表示无显式计划，不注入
    plan = state.get("plan") or {}

    # 只送审“会被入库的达标候选”：先按整理节点的相关性阈值过滤，
    # 再按价值分降序取前 N 条。若按采集顺序取前 N 条，会把整理阶段
    # 本就丢弃的低分条目纳入评分，无谓拉低整体相关性而误判。
    threshold = _review_threshold(plan)
    candidates = [
        a for a in analyses if float(a.get("score", 0.0) or 0.0) >= threshold
    ]
    candidates.sort(
        key=lambda a: float(a.get("score", 0.0) or 0.0), reverse=True
    )
    to_review = candidates[:MAX_REVIEW_ITEMS]
    if not to_review:
        # 无达标候选：整理阶段同样会全部过滤，直接放行空转
        print(f"[Review] 无 >= {threshold} 的候选条目，跳过审核")
        return {
            "review_passed": True,
            "review_feedback": "",
            "iteration": iteration,
            "cost_tracker": tracker,
        }
    if len(candidates) > MAX_REVIEW_ITEMS:
        print(
            f"[Review] 达标候选 {len(candidates)} 条，仅送审前 "
            f"{MAX_REVIEW_ITEMS} 条（控 token 消耗）"
        )
    plan_section = (
        f"审核背景（任务计划）：\n"
        f"{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
        if plan
        else ""
    )
    prompt = (
        f"{plan_section}"
        f"待审核的分析结果列表（JSON）：\n"
        f"{json.dumps(to_review, ensure_ascii=False, indent=2)}"
    )

    try:
        result, usage = chat_json(
            prompt,
            system_prompt=REVIEWER_SYSTEM_PROMPT,
            temperature=REVIEW_TEMPERATURE,
            node_name="reviewer",
        )
        accumulate_usage(tracker, usage)
    except LLMError as exc:
        # 审核挂了不应阻塞流程：降级放行并留痕
        logger.warning("审核 LLM 调用失败，降级放行：%s", exc)
        return {
            "review_passed": True,
            "review_feedback": "",
            "iteration": iteration,
            "cost_tracker": tracker,
        }

    scores = result.get("scores", {})
    if not isinstance(scores, dict):
        logger.warning("模型返回的 scores 非字典：%r，按空分处理", scores)
        scores = {}
    total = _weighted_total(scores)
    passed = total >= PASS_THRESHOLD
    feedback = str(result.get("feedback", "")).strip()
    print(
        f"[Review] 加权总分={total:.2f}（阈值 {PASS_THRESHOLD}） "
        f"passed={passed} scores={scores}"
    )

    return {
        "review_passed": passed,
        "review_feedback": "" if passed else feedback,
        "iteration": iteration,
        "cost_tracker": tracker,
    }


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from workflows.model_client import LLMConfigError
    from workflows.state import make_initial_state

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. 纯本地测试：加权总分重算（不发网络请求）
    sample = {
        "summary_quality": 8,
        "technical_depth": 7,
        "relevance": 9,
        "originality": 7,
        "formatting": 8,
    }
    logger.info("加权总分测试：%s -> %.2f", sample, _weighted_total(sample))

    # 2. 真实调用测试（需配置 LLM_PROVIDER 与对应 API Key）
    state = make_initial_state()
    state["analyses"] = [
        {
            "url": "https://github.com/langchain-ai/langgraph",
            "category": "智能体",
            "tags": ["LangGraph", "多智能体", "工作流"],
            "key_points": ["基于图的智能体编排框架", "支持循环与条件分支"],
            "score": 0.9,
            "summary": "LangGraph 是基于图结构的智能体编排框架，"
            "适合需要构建带循环、返工等复杂控制流的多智能体应用的开发者。",
        }
    ]
    try:
        update = review_node(state)
        print(f"\n审核结果：{update['review_passed']} cost={update['cost_tracker']}")
    except LLMConfigError as exc:
        logger.warning("跳过真实调用测试（配置缺失）：%s", exc)
