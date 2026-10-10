"""LangGraph 工作流的共享状态定义。

整条流水线的各个节点通过 :class:`KBState` 传递数据，数据流向::

    plan → sources → analyses → review ─[pass]→ organize → END
                                  ↓
                                revise → review（循环）
                                  ↓[>max]
                                human_flag → END

设计遵循 **报告式通信** 原则：

- 每个字段是上游节点产出的 **结构化摘要**，而非原始网页/API 响应；
- 下游节点只依赖字段约定的格式，不关心上游的实现细节；
- 大体积原始数据落盘到 ``knowledge/raw/``，状态里只保留精炼后的条目。

Example:
    >>> from workflows import KBState, make_initial_state
    >>> state = make_initial_state()
    >>> state["iteration"]
    0
"""

from __future__ import annotations

from typing import Any, TypedDict

# 审核循环的最大次数：超过后不再返工，直接带反馈结束流程
MAX_REVIEW_ITERATIONS: int = 3


class KBState(TypedDict):
    """知识库构建工作流的共享状态。

    所有节点读写同一份状态；每个字段由特定节点负责写入，
    其余节点只读，避免多写冲突。
    """

    # 本次构建的任务计划（Planner 节点写入，位于数据流最上游）。
    # 驱动采集节点的目标与范围，并作为审核节点的背景上下文。
    # 格式（见 workflows/planner.py 的三档策略）：
    #   {"mode": "lite" | "standard" | "full", "target_count": int,
    #    "per_source_limit": int, "relevance_threshold": float,
    #    "max_iterations": int, "rationale": str}
    # 空 dict 表示无显式计划，各节点使用自身默认配置。
    plan: dict[str, Any]

    # 采集到的原始数据摘要（采集节点写入）。
    # 每项为一条来源记录，格式：
    #   {"source": "github" 或 RSS 源名（rss_sources.yaml 中的 name，
    #    如 "openai-blog"）, "title": str, "url": str,
    #    "summary": str, "author": str, "published_at": ISO 8601 字符串,
    #    "collected_at": ISO 8601 字符串}
    # author / published_at 供整理节点组装 AGENTS.md 知识条目复用。
    # 注意：这里存放的是清洗后的摘要字段，完整原始响应落盘到
    # knowledge/raw/，不进入状态。
    sources: list[dict[str, Any]]

    # LLM 分析后的结构化结果（分析节点写入）。
    # 每项与 sources 中的条目一一对应，格式：
    #   {"url": str, "category": "agent"|"llm"|"rag"|"tool"|"other",
    #    "tags": list[str], "key_points": list[str], "score": float,
    #    "summary": str, "abstract": str, "language": "zh"|"en"}
    # score 为 0~1 的价值评分，供整理节点过滤低质条目。
    analyses: list[dict[str, Any]]

    # 整理、去重后的知识条目（整理节点写入）。
    # 每项为最终可入库的条目，结构与 AGENTS.md「知识条目 JSON
    # 格式」一致：
    #   {"id": "YYYYMMDD-{source}-{序号}", "title": str, "source": str,
    #    "source_url": str, "summary": str, "abstract": str,
    #    "tags": list[str], "category": str, "language": "zh"|"en",
    #    "author": str（可选）, "published_at": ISO 8601,
    #    "fetched_at": ISO 8601, "status": "reviewed",
    #    "quality_score": float（1.0~5.0）}
    # 以 source_url 作为去重键，重复来源只保留评分最高的一条。
    articles: list[dict[str, Any]]

    # 审核反馈意见（审核节点写入）。
    # 未通过时为具体的修改建议文本，供格式化节点返工参考；
    # 通过时为空字符串。
    review_feedback: str

    # 审核是否通过（审核节点写入）。
    # True 表示 articles 可以落盘；False 表示需要返工或已达
    # 最大循环次数后放弃。
    review_passed: bool

    # 当前审核循环次数（审核节点自增，初始为 0）。
    # 达到 MAX_REVIEW_ITERATIONS 后即使未通过也强制结束，
    # 防止 审核 <-> 返工 之间死循环。
    iteration: int

    # 是否需要人工介入（HumanFlag 节点写入，初始为 False）。
    # 审核循环超过上限仍未通过时置 True，问题条目写入
    # knowledge/pending_review/ 等待人工判断，不进主知识库。
    needs_human_review: bool

    # Token 用量追踪（所有调用 LLM 的节点累加）。
    # 格式：
    #   {"prompt_tokens": int, "completion_tokens": int,
    #    "total_tokens": int, "calls": int}
    # calls 为累计 LLM 调用次数，用于估算单次运行成本。
    cost_tracker: dict[str, int]


def make_initial_state() -> KBState:
    """构造一份空的初始状态，供图的入口节点使用。

    Returns:
        各字段均为零值的 :class:`KBState`。
    """
    return KBState(
        plan={},
        sources=[],
        analyses=[],
        articles=[],
        review_feedback="",
        review_passed=False,
        iteration=0,
        needs_human_review=False,
        cost_tracker={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        },
    )
