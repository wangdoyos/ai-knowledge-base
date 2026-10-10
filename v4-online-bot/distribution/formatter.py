"""Formatter 分发格式化模块：知识条目 → 各渠道消息体。

把 AGENTS.md「知识条目 JSON 格式」的单篇文章（见
``knowledge/articles/{id}.json``：``id / title / source / source_url /
summary / abstract / tags / category / language / author /
published_at / fetched_at / status / quality_score``）格式化为三种
分发渠道的消息体：

1. **Markdown**：通用图文格式（邮件 / 文档 / 网页嵌入）；
2. **QQ 纯文本**：QQ 群消息不支持富文本，用符号前缀分行排版；
3. **飞书 interactive 卡片**：header 颜色随相关性评分染色。

字段兼容：采集态条目用 ``collected_at``（0~1 的 ``relevance_score``），
落库态条目用 ``fetched_at``（1.0~5.0 的 ``quality_score``），本模块
两者都接受——日期优先取 ``collected_at``，评分优先取
``relevance_score``，缺失时把 ``quality_score`` 反向映射回 0~1。

本模块是纯函数集合，不发任何网络请求（网络分发归 publisher.py）。

Example:
    >>> from distribution.formatter import generate_daily_digest
    >>> digest = generate_daily_digest(date="19700101")  # 无文章的日期
    >>> "暂无新增知识条目" in digest["markdown"]
    True
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 项目根目录（v4-online-bot/），直接运行本文件时也能 import distribution 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 默认知识库目录（generate_daily_digest 的扫描目标）
DEFAULT_KNOWLEDGE_DIR = ROOT_DIR / "knowledge" / "articles"

# 相关性分档阈值（0~1）：高 / 中 / 低
RELEVANCE_HIGH = 0.8
RELEVANCE_MEDIUM = 0.6

# 各分档对应的展示符号与飞书 header 颜色
RELEVANCE_BADGES = {"high": "⭐⭐⭐", "medium": "⭐⭐", "low": "⭐"}
FEISHU_TEMPLATES = {"high": "green", "medium": "yellow", "low": "red"}

# quality_score 的值域（AGENTS.md 约定 1.0~5.0），用于反向映射回 0~1
QUALITY_SCORE_MIN = 1.0
QUALITY_SCORE_MAX = 5.0


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _relevance_score(article: dict[str, Any]) -> float:
    """取文章的 0~1 相关性评分，兼容采集态与落库态两种字段。

    优先读采集态的 ``relevance_score``（0~1）；缺失时把落库态的
    ``quality_score``（1.0~5.0）线性反向映射回 0~1。

    Args:
        article: 单篇知识条目 dict。

    Returns:
        截断到 0~1 区间的相关性评分；两个字段都缺失时为 0.0。
    """
    if "relevance_score" in article:
        score = float(article.get("relevance_score", 0.0))
    else:
        quality = float(article.get("quality_score", QUALITY_SCORE_MIN))
        score = (quality - QUALITY_SCORE_MIN) / (QUALITY_SCORE_MAX - QUALITY_SCORE_MIN)
    return min(max(score, 0.0), 1.0)


def _relevance_level(score: float) -> str:
    """按分档阈值把 0~1 评分归入 high / medium / low 档。

    Args:
        score: 0~1 的相关性评分。

    Returns:
        ``"high"``（>= 0.8）、``"medium"``（>= 0.6）或 ``"low"``。
    """
    if score >= RELEVANCE_HIGH:
        return "high"
    if score >= RELEVANCE_MEDIUM:
        return "medium"
    return "low"


def _article_date(article: dict[str, Any]) -> str:
    """取文章日期（``YYYY-MM-DD``），截时间字符串前 10 位。

    优先取采集态的 ``collected_at``，缺失时依次回退
    ``fetched_at`` / ``published_at``。

    Args:
        article: 单篇知识条目 dict。

    Returns:
        ``YYYY-MM-DD`` 格式日期；全部缺失时为空字符串。
    """
    for field in ("collected_at", "fetched_at", "published_at"):
        value = str(article.get(field, "")).strip()
        if value:
            return value[:10]
    return ""


# ---------------------------------------------------------------------------
# 单篇格式化
# ---------------------------------------------------------------------------


def json_to_markdown(article: dict[str, Any]) -> str:
    """把单篇知识条目格式化为通用 Markdown 文本。

    包含标题、来源、日期（时间字符串前 10 位）、相关性评分
    （>= 0.8 ⭐⭐⭐ / >= 0.6 ⭐⭐ / 否则 ⭐）、标签、summary 与原文链接。

    Args:
        article: 符合 AGENTS.md 知识条目格式的单篇文章 dict。

    Returns:
        Markdown 格式的文章内容。
    """
    score = _relevance_score(article)
    badge = RELEVANCE_BADGES[_relevance_level(score)]
    tags = " ".join(f"`{tag}`" for tag in article.get("tags", []))
    title = str(article.get("title", "")).strip()
    source = str(article.get("source", "")).strip()
    date = _article_date(article).strip()
    url = str(article.get("source_url", "")).strip()
    abstract = str(article.get("abstract", "") or "").strip()
    summary = str(article.get("summary", "") or "").strip()

    lines = [f"## 📖 {title}", ""]
    # 引言：优先 abstract，缺省时取 summary 首句
    lead = abstract or (summary.split("。")[0] + "。" if summary else "")
    if lead:
        lines += [f"> {lead}", ""]
    lines += ["---", ""]
    # 元信息块
    meta: list[str] = []
    if source:
        meta.append(f"📌 **来源**：{source}")
    if date:
        meta.append(f"📅 **日期**：{date}")
    meta.append(f"⭐ **相关性**：{badge}（{score:.2f}）")
    lines += ["　".join(meta), ""]
    if tags:
        lines += [f"🏷️ **标签**：{tags}", ""]
    lines += ["---", ""]
    # 正文
    if summary:
        lines += [summary, ""]
    # 原文链接单独成行
    if url:
        lines += [f"🔗 [阅读原文]({url})"]
    return "\n".join(lines)


def json_to_qq(article: dict[str, Any]) -> str:
    """把单篇知识条目格式化为 QQ 纯文本消息。

    QQ 群消息不支持富文本，用符号前缀分行排版；原文链接单独
    成行便于点击，tag 中的空格替换为下划线并加 ``#`` 前缀。

    Args:
        article: 符合 AGENTS.md 知识条目格式的单篇文章 dict。

    Returns:
        QQ 纯文本格式的消息。
    """
    score = _relevance_score(article)
    badge = RELEVANCE_BADGES[_relevance_level(score)]
    hashtags = " ".join(
        f"#{str(tag).replace(' ', '_')}" for tag in article.get("tags", [])
    )
    lines = [
        f"📖 {article.get('title', '')}",
        "",
        str(article.get("summary", "")),
        "",
        f"{badge} 相关性：{score:.2f}",
        f"📌 来源：{article.get('source', '')}",
        f"🏷️ {hashtags}",
        f"🔗 原文：{article.get('source_url', '')}",
    ]
    return "\n".join(lines)


def json_to_feishu(article: dict[str, Any]) -> dict[str, Any]:
    """把单篇知识条目格式化为飞书 interactive 卡片消息体。

    ``msg_type`` 固定为 ``interactive``，header 颜色按相关性评分
    染色：>= 0.8 green / >= 0.6 yellow / 否则 red。

    Args:
        article: 符合 AGENTS.md 知识条目格式的单篇文章 dict。

    Returns:
        可直接 POST 给飞书机器人 webhook 的消息体 dict。
    """
    score = _relevance_score(article)
    level = _relevance_level(score)
    tags = " ".join(f"#{tag}" for tag in article.get("tags", []))
    meta = (
        f"**来源**：{article.get('source', '')}　"
        f"**日期**：{_article_date(article)}　"
        f"**相关性**：{RELEVANCE_BADGES[level]}（{score:.2f}）"
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": FEISHU_TEMPLATES[level],
                "title": {"tag": "plain_text", "content": str(article.get("title", ""))},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": meta}},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": str(article.get("summary", ""))},
                },
                {"tag": "div", "text": {"tag": "lark_md", "content": tags}},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "阅读原文"},
                            "type": "primary",
                            "url": str(article.get("source_url", "")),
                        }
                    ],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# 当日简报
# ---------------------------------------------------------------------------


def _load_articles(knowledge_dir: Path, date: str) -> list[dict[str, Any]]:
    """扫描知识库目录中指定日期的文章并按相关性降序排序。

    文件名约定为 ``{date}-{source}-{seq}.json``（如
    ``20260817-github-002.json``），用 ``Path.glob`` 匹配；坏文件
    跳过并记警告，不中断简报生成。

    Args:
        knowledge_dir: 知识库文章目录。
        date: ``YYYYMMDD`` 格式的日期前缀。

    Returns:
        按相关性评分降序排列的文章列表。
    """
    articles: list[dict[str, Any]] = []
    for path in sorted(knowledge_dir.glob(f"{date}-*.json")):
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("跳过无法解析的知识条目：%s（%s）", path.name, exc)
            continue
        if isinstance(article, dict):
            articles.append(article)
    return sorted(articles, key=_relevance_score, reverse=True)


def _empty_feishu_card(message: str, date: str) -> dict[str, Any]:
    """构造当日无文章时的飞书占位卡片。

    Args:
        message: 空简报提示文案。
        date: ``YYYYMMDD`` 格式的日期。

    Returns:
        灰色 header 的飞书 interactive 卡片 dict。
    """
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "grey",
                "title": {"tag": "plain_text", "content": f"📰 AI 知识简报 · {date}"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": message}}
            ],
        },
    }


def generate_daily_digest(
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_DIR,
    date: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """生成当日知识简报，覆盖 Markdown / QQ / 飞书三种渠道。

    用 ``Path.glob(f"{date}-*.json")`` 扫描知识库目录，按相关性
    评分降序取 Top N 组装简报。

    Args:
        knowledge_dir: 知识库文章目录，默认 ``knowledge/articles``。
        date: ``YYYYMMDD`` 格式日期，``None`` 时取今天。
        top_n: 简报收录的文章数上限（默认 10）。

    Returns:
        ``{"markdown": str, "qq": str, "feishu": dict}``；当日
        无文章时 markdown / qq 为 ``"📭 {date} 暂无新增知识
        条目"`` 提示，feishu 为灰色占位卡片。
    """
    date = date or datetime.now().strftime("%Y%m%d")
    articles = _load_articles(Path(knowledge_dir), date)[:top_n]

    if not articles:
        message = f"📭 {date} 暂无新增知识条目"
        return {
            "markdown": message,
            "qq": message,
            "feishu": _empty_feishu_card(message, date),
        }

    heading = f"📰 AI 知识简报 · {date}"

    markdown = "\n\n---\n\n".join(
        [f"# {heading}\n\n共收录 {len(articles)} 篇（按相关性降序）"]
        + [json_to_markdown(article) for article in articles]
    )

    qq = f"\n\n{'—' * 12}\n\n".join(
        [f"{heading}\n共收录 {len(articles)} 篇（按相关性降序）"]
        + [json_to_qq(article) for article in articles]
    )

    # 飞书汇总为单张卡片：header 颜色跟随榜首文章的相关性分档
    top_level = _relevance_level(_relevance_score(articles[0]))
    elements: list[dict[str, Any]] = []
    for i, article in enumerate(articles, start=1):
        if i > 1:
            elements.append({"tag": "hr"})
        score = _relevance_score(article)
        badge = RELEVANCE_BADGES[_relevance_level(score)]
        content = (
            f"**{i}. [{article.get('title', '')}]({article.get('source_url', '')})**\n"
            f"{article.get('abstract') or article.get('summary', '')}\n"
            f"{badge}（{score:.2f}）　来源：{article.get('source', '')}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})
    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"共收录 {len(articles)} 篇 · 按相关性降序"}
            ],
        }
    )
    feishu = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": FEISHU_TEMPLATES[top_level],
                "title": {"tag": "plain_text", "content": heading},
            },
            "elements": elements,
        },
    }

    return {"markdown": markdown, "qq": qq, "feishu": feishu}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # 从知识库挑最新日期的真实文章做输出验证（index.json 不是知识条目，排除）
    entry_paths = sorted(
        p for p in DEFAULT_KNOWLEDGE_DIR.glob("*.json") if p.name != "index.json"
    )
    if not entry_paths:
        print(f"知识库目录暂无文章：{DEFAULT_KNOWLEDGE_DIR}")
        sys.exit(0)

    latest_date = entry_paths[-1].name.split("-", 1)[0]  # id 前缀即 YYYYMMDD
    sample = json.loads(entry_paths[-1].read_text(encoding="utf-8"))
    print(f"知识库共 {len(entry_paths)} 篇，最新日期 {latest_date}，"
          f"样例文章：{entry_paths[-1].name}")

    print("\n========== json_to_markdown ==========")
    print(json_to_markdown(sample))

    print("\n========== json_to_qq ==========")
    print(json_to_qq(sample))

    print("\n========== json_to_feishu ==========")
    print(json.dumps(json_to_feishu(sample), ensure_ascii=False, indent=2))

    print(f"\n========== generate_daily_digest({latest_date}) ==========")
    digest = generate_daily_digest(date=latest_date)
    print(digest["markdown"])
    print("\n---------- qq 版 ----------")
    print(digest["qq"])
    print("\n---------- feishu 卡片 ----------")
    print(json.dumps(digest["feishu"], ensure_ascii=False, indent=2))
