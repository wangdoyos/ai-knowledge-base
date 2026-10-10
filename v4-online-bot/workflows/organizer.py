"""Organizer 智能体节点：整理入库（正常终点）。

审核通过后的收尾环节（analyses → articles → 落盘）：

1. **整理**：过滤评分低于相关性阈值（plan 优先）的条目、按 URL
   去重（重复只保留评分最高的一条）、按评分降序编号，组装为
   AGENTS.md 「知识条目 JSON 格式」约定的 articles：
   ``id / title / source / source_url / summary / abstract / tags /
   category / language / author / published_at / fetched_at / status /
   quality_score``，其中 quality_score 由 0~1 的分析评分映射到
   1.0~5.0，status 固定为 ``reviewed``（已过 Reviewer 审核）；
2. **入库**：每篇文章写为独立的 ``knowledge/articles/{id}.json``，
   并与既有 ``index.json`` 按 URL 合并去重。

安全接入：入库前对对外展示的文本字段（title / summary /
abstract）调用 :func:`tests.security.secure_output` 做 PII 检测
与掩码（手机号/邮箱/身份证/信用卡/IP → ``[TYPE_MASKED]``），
防止 LLM 输出携带的敏感信息随知识库外泄。

本节点是纯本地逻辑，不调用 LLM；审核返工由 Reviser 在审核环节
之前完成，这里只处理已通过审核的 analyses。

Example:
    >>> from workflows.organizer import organize_node
    >>> from workflows.state import make_initial_state
    >>> update = organize_node(make_initial_state())  # 空输入自然短路
    >>> update["articles"]
    []
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tests.security import secure_output  # noqa: E402
from workflows.state import KBState  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 主知识库落盘目录与索引文件
ARTICLES_DIR = ROOT_DIR / "knowledge" / "articles"
INDEX_PATH = ARTICLES_DIR / "index.json"

# 无显式计划时的默认相关性阈值（分析评分为 0~1）；
# plan.relevance_threshold 优先，低于阈值的条目被过滤
SCORE_THRESHOLD = 0.6

# 落库条目状态：organize 在 Reviewer 审核通过后执行，对应
# AGENTS.md 状态流转 draft → reviewed → published 中的 reviewed
ARTICLE_STATUS_REVIEWED = "reviewed"

# quality_score 的值域（AGENTS.md 约定 1.0~5.0），由 0~1 分析评分线性映射
QUALITY_SCORE_MIN = 1.0
QUALITY_SCORE_MAX = 5.0


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _relevance_threshold(state: KBState) -> float:
    """从 plan 读相关性阈值，无显式计划时用默认值。

    Args:
        state: 读取 ``plan`` 的 ``relevance_threshold``。

    Returns:
        0~1 的相关性阈值。
    """
    return float(state["plan"].get("relevance_threshold", SCORE_THRESHOLD))


def _to_iso8601(value: str) -> str:
    """把时间字符串规整为带时区的 ISO 8601 格式。

    Args:
        value: ISO 8601 或 ``YYYY-MM-DD HH:MM:SS`` 格式的时间字符串。

    Returns:
        带时区的 ISO 8601 字符串；无法解析时回退为当前本地时间。
    """
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        dt = datetime.now()
    if dt.tzinfo is None:
        # 无时区信息时按本地时区补齐
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def _quality_score(score: float) -> float:
    """把 0~1 的分析评分线性映射为 AGENTS.md 约定的 1.0~5.0 质量分。

    Args:
        score: 分析节点产出的 0~1 价值评分，越界值先截断。

    Returns:
        保留一位小数的 1.0~5.0 质量分。
    """
    clamped = min(max(score, 0.0), 1.0)
    return round(
        QUALITY_SCORE_MIN + clamped * (QUALITY_SCORE_MAX - QUALITY_SCORE_MIN), 1
    )


def _merge_articles(state: KBState) -> list[dict[str, Any]]:
    """把 sources 与 analyses 按 URL 合并成 AGENTS.md 格式的 articles。

    过滤评分低于相关性阈值（plan 优先，默认
    :data:`SCORE_THRESHOLD`）的条目；重复 URL 只保留
    评分最高的一条。每条按「知识条目 JSON 格式」组装：
    id 为 ``{date}-{source}-{seq}``（序号三位），author 为可选
    字段（来源缺失时省略），published_at 缺失时回退为采集时间。

    Args:
        state: 读取 ``plan`` / ``sources`` 与 ``analyses``。

    Returns:
        按评分降序、符合 AGENTS.md 知识条目格式的 articles 列表。
    """
    threshold = _relevance_threshold(state)
    sources_by_url = {s["url"]: s for s in state["sources"]}

    best_by_url: dict[str, dict[str, Any]] = {}
    for analysis in state["analyses"]:
        url = analysis.get("url", "")
        score = float(analysis.get("score", 0.0))
        if url not in sources_by_url:
            continue
        if score < threshold:
            logger.info("过滤低分条目：url=%s score=%.2f", url, score)
            continue
        existing = best_by_url.get(url)
        if existing is None or score > float(existing.get("score", 0.0)):
            best_by_url[url] = analysis

    ranked = sorted(
        best_by_url.values(), key=lambda a: float(a.get("score", 0.0)), reverse=True
    )
    date_prefix = datetime.now().strftime("%Y%m%d")
    articles: list[dict[str, Any]] = []
    for i, analysis in enumerate(ranked, start=1):
        source = sources_by_url[analysis["url"]]
        fetched_at = _to_iso8601(str(source.get("collected_at", "")))
        article: dict[str, Any] = {
            "id": f"{date_prefix}-{source['source']}-{i:03d}",
            "title": source["title"],
            "source": source["source"],
            "source_url": analysis["url"],
            "summary": analysis.get("summary", ""),
            "abstract": analysis.get("abstract", ""),
            "tags": analysis.get("tags", []),
            "category": analysis.get("category", "other"),
            "language": analysis.get("language", "zh"),
        }
        author = str(source.get("author", "")).strip()
        if author:
            article["author"] = author
        published_at = str(source.get("published_at", "")).strip()
        article["published_at"] = _to_iso8601(published_at) if published_at else fetched_at
        article["fetched_at"] = fetched_at
        article["status"] = ARTICLE_STATUS_REVIEWED
        article["quality_score"] = _quality_score(float(analysis.get("score", 0.0)))
        articles.append(article)
    return articles


def _filter_pii(articles: list[dict[str, Any]]) -> int:
    """入库前对对外展示的文本字段做 PII 检测与掩码（原地修改）。

    覆盖 title / summary / abstract 三类字段：summary 与 abstract
    是 LLM 生成文本（主要泄露面），title 来自外部采集内容。
    命中的 PII 替换为 ``[TYPE_MASKED]`` 并记入审计日志。

    Args:
        articles: 待入库的条目列表，字段原地替换为掩码后文本。

    Returns:
        全部条目累计的 PII 命中数。
    """
    total_detections = 0
    for article in articles:
        for field in ("title", "summary", "abstract"):
            filtered, detections = secure_output(str(article.get(field, "")))
            article[field] = filtered
            total_detections += len(detections)
    if total_detections:
        logger.warning("入库前 PII 过滤：共掩码 %d 处敏感信息", total_detections)
    return total_detections


def _save_articles(articles: list[dict[str, Any]]) -> None:
    """articles 写入 ``knowledge/articles/`` 并更新索引。

    每篇文章按 AGENTS.md 知识条目格式写为独立的 ``{id}.json``；
    ``index.json`` 与既有内容按 URL 合并去重（兼容旧条目的
    ``url`` 键，新条目用 ``source_url``，新覆盖旧），格式为
    ``{"articles": [...], "updated_at": "..."}``，与
    ``patterns/router.py`` 的知识库检索处理器兼容。

    Args:
        articles: 整理完成、待入库的条目列表（非空）。
    """
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    for article in articles:
        path = ARTICLES_DIR / f"{article['id']}.json"
        path.write_text(
            json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 与既有索引按 URL 合并去重，新条目覆盖旧条目；
    # 旧索引用 url 键、新索引用 source_url 键，合并时两者都认
    existing: list[dict[str, Any]] = []
    if INDEX_PATH.exists():
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            existing = data.get("articles", []) if isinstance(data, dict) else data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("既有索引读取失败，将重建：%s", exc)

    merged: dict[str, dict[str, Any]] = {
        str(a.get("source_url") or a.get("url") or a.get("id", "")): a
        for a in existing
    }
    for article in articles:
        merged[article["source_url"]] = {
            "id": article["id"],
            "title": article["title"],
            "source": article["source"],
            "source_url": article["source_url"],
            "category": article["category"],
            "tags": article["tags"],
            "abstract": article["abstract"],
            "quality_score": article["quality_score"],
            "status": article["status"],
        }

    index = {
        "articles": list(merged.values()),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[Organize] 入库完成：索引共 {len(merged)} 条 -> {INDEX_PATH.name}")


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


def organize_node(state: KBState) -> dict[str, Any]:
    """Organizer 节点：整理入库，工作流的正常终点。

    先过滤低分、按 URL 去重，组装为 AGENTS.md 知识条目格式的
    articles，再经 PII 掩码后落盘主知识库并更新索引；无候选
    条目时跳过落盘，流程自然空转结束。

    Args:
        state: 读取 ``plan`` / ``sources`` / ``analyses``。

    Returns:
        ``{"articles": [...]}``（文本字段已完成 PII 掩码）。
    """
    articles = _merge_articles(state)
    print(
        f"[Organize] 整理完成：{len(state['analyses'])} 条分析 -> "
        f"{len(articles)} 条候选（阈值 {_relevance_threshold(state)}，按 URL 去重）"
    )

    if not articles:
        print("[Organize] 无条目可入库，跳过落盘")
        return {"articles": articles}

    detections = _filter_pii(articles)
    if detections:
        print(f"[Organize] 安全过滤：已掩码 {detections} 处 PII")
    _save_articles(articles)
    return {"articles": articles}


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from workflows.state import make_initial_state

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 纯本地测试：空输入短路（不写盘）
    update = organize_node(make_initial_state())
    assert update["articles"] == []
    logger.info("空输入短路测试通过")

    # 阈值过滤测试：0.5 分条目在默认阈值 0.6 下被过滤（不写盘）
    state = make_initial_state()
    state["sources"] = [
        {
            "source": "github",
            "title": "example/low-score",
            "url": "https://github.com/example/low-score",
            "summary": "测试",
            "author": "example",
            "published_at": "2026-08-01T00:00:00+08:00",
            "collected_at": "2026-08-15T00:00:00+08:00",
        }
    ]
    state["analyses"] = [
        {
            "url": "https://github.com/example/low-score",
            "category": "other",
            "tags": [],
            "key_points": [],
            "score": 0.5,
            "summary": "低分条目",
            "abstract": "低分条目",
            "language": "zh",
        }
    ]
    update = organize_node(state)
    assert update["articles"] == []
    logger.info("阈值过滤测试通过")

    # 评分映射测试：0~1 分析评分 → 1.0~5.0 质量分
    assert _quality_score(0.0) == 1.0
    assert _quality_score(0.5) == 3.0
    assert _quality_score(1.0) == 5.0
    logger.info("quality_score 映射测试通过")
