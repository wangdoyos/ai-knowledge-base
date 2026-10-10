"""Analyzer 智能体节点：LLM 逐条分析来源，生成摘要、标签与评分。

对采集到的每条来源单独调用一次 LLM（sources → analyses），产出
结构化分析结果：详细摘要（summary）、核心摘要（abstract）、分类
（AGENTS.md 枚举：agent/llm/rag/tool/other）、标签、关键要点、
语言（zh/en）与 0~1 的价值评分，供 Organizer 组装为 AGENTS.md
约定的知识条目 JSON。单条分析失败只跳过该条，不中断整个批次。

Example:
    >>> from workflows.analyzer import analyze_node
    >>> from workflows.state import make_initial_state
    >>> update = analyze_node(make_initial_state())  # sources 为空则短路
    >>> update["analyses"]
    []
"""

from __future__ import annotations

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

# 分类与语言枚举，与 AGENTS.md 知识条目 JSON 格式约定一致；
# 模型返回越界值时回退到 DEFAULT_CATEGORY / DEFAULT_LANGUAGE
VALID_CATEGORIES: frozenset[str] = frozenset({"agent", "llm", "rag", "tool", "other"})
DEFAULT_CATEGORY = "other"
VALID_LANGUAGES: frozenset[str] = frozenset({"zh", "en"})
DEFAULT_LANGUAGE = "zh"

ANALYZE_SYSTEM_PROMPT = """你是一个技术知识库的内容分析师。针对给定的开源仓库信息，
生成结构化分析结果，返回 JSON，格式：

{
  "summary": "200~400 字的中文详细摘要（不超过 1500 字），说明项目是什么、解决什么问题、核心特性、适合谁用",
  "abstract": "不超过 200 字的核心摘要，一两句话概括项目价值，用于推送展示",
  "category": "分类，从以下选择一个：agent / llm / rag / tool / other",
  "tags": ["3~5 个中文或通用英文标签"],
  "key_points": ["2~4 条关键要点，每条一句话"],
  "language": "原始内容的主要语言：zh / en",
  "score": 0.85
}

score 为 0~1 的价值评分：与 AI/LLM/Agent 主题的相关性、信息完整度越高分越高。
只返回 JSON 对象，不要输出其他内容。"""


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


def analyze_node(state: KBState) -> dict[str, Any]:
    """分析节点：LLM 逐条生成详细/核心摘要、分类、标签、语言与评分。

    category 与 language 按 AGENTS.md 枚举校验，越界值回退默认；
    单条分析失败只跳过该条，不中断整个批次。

    Args:
        state: 读取 ``sources`` 与 ``cost_tracker``。

    Returns:
        ``{"analyses": [...], "cost_tracker": {...}}``。
    """
    sources = state["sources"]
    print(f"[Analyze] 开始分析 {len(sources)} 条来源")

    tracker = dict(state["cost_tracker"])
    analyses: list[dict[str, Any]] = []
    for i, source in enumerate(sources, start=1):
        prompt = (
            f"仓库名：{source['title']}\n"
            f"链接：{source['url']}\n"
            f"简介：{source['summary']}"
        )
        try:
            result, usage = chat_json(
                prompt,
                system_prompt=ANALYZE_SYSTEM_PROMPT,
                node_name="analyzer",
            )
        except LLMError as exc:
            logger.warning("第 %d 条分析失败，跳过：%s", i, exc)
            continue
        accumulate_usage(tracker, usage)
        category = str(result.get("category", DEFAULT_CATEGORY)).strip().lower()
        if category not in VALID_CATEGORIES:
            logger.warning("第 %d 条分类越界：%r，回退为 %s", i, category, DEFAULT_CATEGORY)
            category = DEFAULT_CATEGORY
        language = str(result.get("language", DEFAULT_LANGUAGE)).strip().lower()
        if language not in VALID_LANGUAGES:
            language = DEFAULT_LANGUAGE
        analyses.append(
            {
                "url": source["url"],
                "category": category,
                "tags": [str(t) for t in result.get("tags", [])],
                "key_points": [str(p) for p in result.get("key_points", [])],
                "score": float(result.get("score", 0.0)),
                "summary": str(result.get("summary", "")),
                "abstract": str(result.get("abstract", "")),
                "language": language,
            }
        )
        print(
            f"[Analyze] {i}/{len(sources)} {source['title']}："
            f"score={analyses[-1]['score']:.2f}"
        )

    print(f"[Analyze] 分析完成：{len(analyses)} 条，累计 tokens={tracker['total_tokens']}")
    return {"analyses": analyses, "cost_tracker": tracker}


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

    # 用一条手工来源做真实调用测试（需配置 LLM_PROVIDER 与对应 API Key）
    state = make_initial_state()
    state["sources"] = [
        {
            "source": "github",
            "title": "langchain-ai/langgraph",
            "url": "https://github.com/langchain-ai/langgraph",
            "summary": "⭐ 10000｜Build resilient language agents as graphs.",
            "collected_at": "2026-08-15 00:00:00",
        }
    ]
    try:
        update = analyze_node(state)
        print(f"\n分析结果：{update['analyses']}")
    except LLMConfigError as exc:
        logger.warning("跳过真实调用测试（配置缺失）：%s", exc)
