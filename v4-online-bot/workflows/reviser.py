"""Reviser 智能体节点：按审核反馈对 analyses 做定向改写。

与 Reviewer（:mod:`workflows.reviewer`）配对使用：审核未通过时，
把 ``review_feedback`` 注入改写提示词，让 LLM 在原 analyses 基础上
定向修改，再交回审核。相比回 analyze 全量重新分析，定向改写
token 消耗更低、且能真正消费反馈意见。

Example:
    >>> from workflows.reviser import revise_node
    >>> from workflows.state import make_initial_state
    >>> revise_node(make_initial_state())  # analyses/feedback 为空，跳过
    {}
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

# 改写任务允许一定创造性（重写摘要、补充要点），温度高于评分任务
REVISE_TEMPERATURE = 0.4

REVISER_SYSTEM_PROMPT = """你是一个知识库编辑。根据审核反馈对分析结果列表做**定向改写**：
重点修正反馈中指出的问题（可以重写 summary 和 abstract、调整 tags 和 key_points），
未提及问题的条目保持原样。不得增删条目，不得修改 url 字段，
每个条目的字段结构必须与输入完全一致
（url / category / tags / key_points / score / summary / abstract / language），
category 只能从 agent / llm / rag / tool / other 中选择。

返回 JSON，格式：{"analyses": [<修改后的完整条目列表>]}
只返回 JSON 对象，不要输出其他内容。"""


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


def revise_node(state: KBState) -> dict[str, Any]:
    """Reviser 节点：把审核反馈注入提示词，定向改写 analyses。

    analyses 或 review_feedback 为空时无事可做，直接返回空更新；
    LLM 调用失败或返回结构异常（非列表、条数不符）时同样返回
    空更新，保留原 analyses 交回审核，不阻塞流程。

    Args:
        state: 读取 ``analyses`` / ``review_feedback`` / ``cost_tracker``。

    Returns:
        ``{"analyses": [...], "cost_tracker": {...}}``；
        跳过或改写失败时返回 ``{}``（状态不变）。
    """
    analyses = state["analyses"]
    feedback = state["review_feedback"].strip()

    if not analyses or not feedback:
        print("[Revise] analyses 或反馈为空，跳过改写")
        return {}

    print(f"[Revise] 按审核反馈改写 {len(analyses)} 条分析结果")
    tracker = dict(state["cost_tracker"])
    prompt = (
        f"审核反馈：\n{feedback}\n\n"
        f"当前分析结果列表（JSON）：\n"
        f"{json.dumps(analyses, ensure_ascii=False, indent=2)}"
    )
    try:
        result, usage = chat_json(
            prompt,
            system_prompt=REVISER_SYSTEM_PROMPT,
            temperature=REVISE_TEMPERATURE,
            node_name="reviser",
        )
        accumulate_usage(tracker, usage)
    except LLMError as exc:
        # 改写挂了不应阻塞流程：保留原 analyses 交回审核
        logger.warning("改写 LLM 调用失败，保留原稿：%s", exc)
        return {}

    improved = result.get("analyses")
    if not isinstance(improved, list) or len(improved) != len(analyses):
        logger.warning("改写结果结构异常（条数不符或非列表），保留原稿")
        return {}

    print(f"[Revise] 定向改写完成：{len(improved)} 条")
    return {"analyses": improved, "cost_tracker": tracker}


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

    # 1. 纯本地测试：空输入跳过（不发网络请求）
    empty = revise_node(make_initial_state())
    assert empty == {}, f"空输入应返回空更新，实际：{empty}"
    logger.info("空输入跳过测试通过")

    # 2. 真实调用测试（需配置 LLM_PROVIDER 与对应 API Key）
    state = make_initial_state()
    state["analyses"] = [
        {
            "url": "https://github.com/langchain-ai/langgraph",
            "category": "agent",
            "tags": ["LangGraph"],
            "key_points": ["一个框架"],
            "score": 0.9,
            "summary": "LangGraph 是个框架。",
            "abstract": "LangGraph 是个框架。",
            "language": "en",
        }
    ]
    state["review_feedback"] = (
        "第 1 条 summary 信息量不足，请补充项目解决什么问题、适合谁用；"
        "key_points 过于空泛，请具体到技术特性。"
    )
    try:
        update = revise_node(state)
        print(f"\n改写结果：{json.dumps(update.get('analyses'), ensure_ascii=False, indent=2)}")
    except LLMConfigError as exc:
        logger.warning("跳过真实调用测试（配置缺失）：%s", exc)
