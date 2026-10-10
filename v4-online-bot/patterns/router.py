"""Router 路由模式：按意图分发用户请求到不同处理器。

核心思路：Router 本身不解决问题，只回答"这个请求该交给谁"。
采用两层意图分类策略平衡成本与准确率：

1. 第一层：关键词快速匹配 —— 零成本、零延迟，命中即返回，不调 LLM；
2. 第二层：LLM 分类兜底 —— 关键词未命中时，让 LLM 判断模糊意图。

支持三种意图，每种意图对应一个处理器函数：

- ``github_search``：调用 GitHub Search API 搜索开源仓库；
- ``knowledge_query``：检索本地知识库 ``knowledge/articles/index.json``；
- ``general_chat``：直接调用 LLM 回答通用问题。

Example:
    >>> from patterns.router import route
    >>> print(route("帮我搜一下 GitHub 上的 RAG 框架"))
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from workflows.model_client import chat, chat_json  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

INTENT_GITHUB_SEARCH = "github_search"
INTENT_KNOWLEDGE_QUERY = "knowledge_query"
INTENT_GENERAL_CHAT = "general_chat"

VALID_INTENTS = (INTENT_GITHUB_SEARCH, INTENT_KNOWLEDGE_QUERY, INTENT_GENERAL_CHAT)

GITHUB_SEARCH_API = "https://api.github.com/search/repositories"
GITHUB_TIMEOUT_SECONDS = 10
GITHUB_RESULT_LIMIT = 5

KNOWLEDGE_INDEX_PATH = ROOT_DIR / "knowledge" / "articles" / "index.json"
KNOWLEDGE_RESULT_LIMIT = 3

# 第一层分类的关键词表：命中任意关键词即判定为对应意图。
# general_chat 不设关键词，作为未命中时的候选交给第二层 LLM 判断。
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    INTENT_GITHUB_SEARCH: (
        "github",
        "开源项目",
        "开源仓库",
        "找个库",
        "搜仓库",
        "repo",
        "repository",
        "star 最多",
    ),
    INTENT_KNOWLEDGE_QUERY: (
        "知识库",
        "文章",
        "收藏",
        "资料库",
        "之前存的",
        "本地检索",
        "knowledge",
    ),
}

CLASSIFY_SYSTEM_PROMPT = """你是一个意图分类器。将用户输入分类为以下三种意图之一：

- github_search：想搜索 GitHub 上的开源项目/仓库/代码库
- knowledge_query：想查询本地知识库中已收录的文章、资料
- general_chat：其他所有情况（闲聊、通用问答、写作等）

只返回 JSON，格式：{"intent": "<意图名>"}，不要输出其他内容。"""


# ---------------------------------------------------------------------------
# 意图分类（两层策略）
# ---------------------------------------------------------------------------


def classify_by_keywords(query: str) -> str | None:
    """第一层：关键词快速匹配，零成本判断意图。

    Args:
        query: 用户输入的原始问题。

    Returns:
        命中的意图名；全部未命中时返回 None（交给第二层处理）。
    """
    lowered = query.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered:
                logger.info("关键词命中：intent=%s keyword=%r", intent, keyword)
                return intent
    return None


def classify_by_llm(query: str) -> str:
    """第二层：LLM 分类兜底，处理关键词覆盖不到的模糊意图。

    Args:
        query: 用户输入的原始问题。

    Returns:
        三种合法意图之一；LLM 调用失败或返回非法值时降级为 general_chat。
    """
    try:
        result = chat_json(
            f"用户输入：{query}",
            system_prompt=CLASSIFY_SYSTEM_PROMPT,
            temperature=0.0,
        )
        # 兼容 chat_json 返回 (dict, usage) 元组的实现
        if isinstance(result, tuple):
            result = result[0]
        intent = str(result.get("intent", "")).strip()
    except Exception as exc:  # noqa: BLE001 分类失败不应中断主流程
        logger.warning("LLM 意图分类失败，降级为 general_chat：%s", exc)
        return INTENT_GENERAL_CHAT

    if intent not in VALID_INTENTS:
        logger.warning("LLM 返回非法意图 %r，降级为 general_chat", intent)
        return INTENT_GENERAL_CHAT
    logger.info("LLM 分类结果：intent=%s", intent)
    return intent


def classify(query: str) -> str:
    """两层意图分类入口：先关键词匹配，未命中再走 LLM。

    Args:
        query: 用户输入的原始问题。

    Returns:
        三种合法意图之一。
    """
    intent = classify_by_keywords(query)
    if intent is not None:
        return intent
    logger.info("关键词未命中，进入 LLM 分类兜底")
    return classify_by_llm(query)


# ---------------------------------------------------------------------------
# 处理器：github_search
# ---------------------------------------------------------------------------


def handle_github_search(query: str) -> str:
    """调用 GitHub Search API 搜索仓库，按 star 数降序取前几名。

    query 参数用 urllib.parse.quote 编码，避免中文与空格导致 URL 非法。

    Args:
        query: 用户输入的原始问题（直接作为搜索词）。

    Returns:
        格式化的搜索结果文本；请求失败时返回错误说明。
    """
    encoded = urllib.parse.quote(query)
    url = (
        f"{GITHUB_SEARCH_API}?q={encoded}"
        f"&sort=stars&order=desc&per_page={GITHUB_RESULT_LIMIT}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-knowledge-base-router",
        },
    )
    logger.info("GitHub 搜索：%s", url)
    try:
        with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return f"GitHub 搜索失败：HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"GitHub 搜索失败：网络错误（{exc}）"
    except json.JSONDecodeError:
        return "GitHub 搜索失败：响应不是合法 JSON"

    items = data.get("items", [])
    if not items:
        return f"GitHub 上没有找到与 {query!r} 相关的仓库。"

    lines = [f"GitHub 搜索 {query!r}，共 {data.get('total_count', 0)} 个结果，前 {len(items)} 名："]
    for i, item in enumerate(items, start=1):
        description = (item.get("description") or "无描述").strip()
        lines.append(
            f"{i}. {item.get('full_name', '未知仓库')}"
            f"（⭐ {item.get('stargazers_count', 0)}）\n"
            f"   {description}\n"
            f"   {item.get('html_url', '')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 处理器：knowledge_query
# ---------------------------------------------------------------------------


def _load_knowledge_index() -> list[dict[str, Any]]:
    """加载本地知识库索引文件。

    Returns:
        文章条目列表；文件不存在或格式异常时返回空列表。
        兼容两种格式：顶层为数组，或 {"articles": [...]} 包装对象。
    """
    if not KNOWLEDGE_INDEX_PATH.exists():
        logger.warning("知识库索引不存在：%s", KNOWLEDGE_INDEX_PATH)
        return []
    try:
        data = json.loads(KNOWLEDGE_INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("知识库索引读取失败：%s", exc)
        return []
    if isinstance(data, dict):
        data = data.get("articles", [])
    return data if isinstance(data, list) else []


def _score_article(query: str, article: dict[str, Any]) -> int:
    """按字段加权计算文章与查询的相关性得分。

    规则：标题命中 +3 / 标签命中 +2 / 摘要命中 +1，
    以查询整体和逐字符（中文）/逐词（英文）两种粒度匹配。

    Args:
        query: 用户输入的原始问题。
        article: 单篇文章的索引条目。

    Returns:
        相关性得分，越大越相关；0 表示不相关。
    """
    title = str(article.get("title", "")).lower()
    tags = " ".join(str(t) for t in article.get("tags", [])).lower()
    summary = str(
        article.get("summary", "") or article.get("abstract", "")
    ).lower()

    # 提取查询词：英文按空格切词，中文取整句 + 2 字以上连续片段简单处理
    terms = {w for w in query.lower().split() if len(w) >= 2}
    terms.add(query.lower().strip())

    score = 0
    for term in terms:
        if term in title:
            score += 3
        if term in tags:
            score += 2
        if term in summary:
            score += 1
    return score


def handle_knowledge_query(query: str) -> str:
    """从本地 knowledge/articles/index.json 检索相关文章。

    Args:
        query: 用户输入的原始问题。

    Returns:
        格式化的检索结果文本；索引缺失或无匹配时返回提示。
    """
    articles = _load_knowledge_index()
    if not articles:
        return f"本地知识库为空或索引缺失（{KNOWLEDGE_INDEX_PATH}），无法检索。"

    scored = [(a, _score_article(query, a)) for a in articles]
    matched = sorted(
        (item for item in scored if item[1] > 0),
        key=lambda item: item[1],
        reverse=True,
    )[:KNOWLEDGE_RESULT_LIMIT]

    if not matched:
        return f"知识库中没有找到与 {query!r} 相关的文章（共 {len(articles)} 篇）。"

    lines = [f"知识库检索 {query!r}，命中 {len(matched)} 篇："]
    for i, (article, score) in enumerate(matched, start=1):
        abstract = str(
            article.get("abstract", "") or article.get("summary", "")
        ).strip()
        tags = "、".join(str(t) for t in article.get("tags", []))
        lines.append(
            f"{i}. {article.get('title', '无标题')}（相关度 {score}）\n"
            f"   标签：{tags or '无'}\n"
            f"   摘要：{abstract[:120] or '无'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 处理器：general_chat
# ---------------------------------------------------------------------------


def handle_general_chat(query: str) -> str:
    """通用问答：直接调用 LLM 回答。

    Args:
        query: 用户输入的原始问题。

    Returns:
        模型回答文本；调用失败时返回错误说明。
    """
    try:
        text, usage = chat(query, temperature=0.7)
    except Exception as exc:  # noqa: BLE001 对外统一返回文本而非抛异常
        logger.error("通用问答 LLM 调用失败：%s", exc)
        return f"LLM 调用失败：{exc}"
    logger.info("通用问答完成：usage=%s", usage)
    return text


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Callable[[str], str]] = {
    INTENT_GITHUB_SEARCH: handle_github_search,
    INTENT_KNOWLEDGE_QUERY: handle_knowledge_query,
    INTENT_GENERAL_CHAT: handle_general_chat,
}


def route(query: str) -> str:
    """路由统一入口：分类意图并分发到对应处理器。

    Args:
        query: 用户输入的原始问题。

    Returns:
        处理器返回的结果文本。
    """
    query = query.strip()
    if not query:
        return "请输入有效的问题。"

    intent = classify(query)
    handler = HANDLERS[intent]
    logger.info("路由分发：query=%r -> intent=%s", query, intent)
    return handler(query)


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    test_queries = [
        "帮我在 GitHub 上搜一下 multi agent framework",  # 关键词命中 github_search
        "知识库里有哪些关于 RAG 的文章？",  # 关键词命中 knowledge_query
        "用一句话解释什么是路由模式",  # 未命中关键词 -> LLM 兜底 general_chat
    ]

    for q in test_queries:
        print(f"\n{'=' * 60}\n问题：{q}\n{'-' * 60}")
        print(route(q))
