#!/usr/bin/env python3
"""每日推送内容准备：群 markdown digest + 私聊成本报告。

- 群：取当天新入库文章（knowledge/articles/{today}-*.json），按
  quality_score 降序取前 MAX_PUSH 条，逐篇用 formatter.json_to_markdown
  生成 markdown，写入 /tmp/kb_push/（manifest.json + 001.md ...）。
  推送前按 knowledge/push_history.json 过滤已推送过的文章（重复不推）；
  历史文件不存在时自动初始化：知识库中非当天入库的文章视为已推送。
- 私聊：读最新 knowledge/cost-report-*.json，格式化为纯文本写入
  /tmp/kb_cost.txt。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from distribution.formatter import json_to_markdown  # noqa: E402

ARTICLES_DIR = ROOT_DIR / "knowledge" / "articles"
MAX_PUSH = 10  # 最大推送条数(老王指定)
MAX_DIGEST_CHARS = 7000  # 单条 markdown 安全长度,超过则降级紧凑摘要版
OUT_DIR = Path("/tmp/kb_push")
COST_OUT = Path("/tmp/kb_cost.txt")

# 推送历史（已推送 URL 集合），用于推送前去重
PUSH_HISTORY = ROOT_DIR / "knowledge" / "push_history.json"


def _load_pushed_urls() -> set[str]:
    """已推送过的 URL 集合；历史文件不存在时自动初始化。

    首次运行（无 push_history.json）时，把知识库中非当天入库的
    文章全部视为已推送——这些是前几天推过的旧内容，避免首日
    推送即重复。当天入库的文章不预标记，正常进入本次推送。

    Returns:
        已推送 URL 的集合。
    """
    if PUSH_HISTORY.exists():
        try:
            data = json.loads(PUSH_HISTORY.read_text(encoding="utf-8"))
            return set(data.get("urls", []))
        except (OSError, json.JSONDecodeError):
            pass

    today = datetime.now().strftime("%Y%m%d")
    pushed: set[str] = set()
    for path in ARTICLES_DIR.glob("*.json"):
        if path.name == "index.json" or path.name.startswith(f"{today}-"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = data.get("source_url") or data.get("url") or ""
        if url:
            pushed.add(url)
    print(f"推送历史初始化：历史文件不存在，将 {len(pushed)} 篇历史文章"
          "标记为已推送")
    # 初始化结果直接落盘，避免同一进程内重复初始化
    PUSH_HISTORY.write_text(
        json.dumps(
            {
                "urls": sorted(pushed),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return pushed


def _record_pushed(articles: list[dict]) -> None:
    """把本次实际推送的文章 URL 记入推送历史（幂等合并）。"""
    pushed = _load_pushed_urls()
    for article in articles:
        url = article.get("source_url") or article.get("url") or ""
        if url:
            pushed.add(url)
    PUSH_HISTORY.write_text(
        json.dumps(
            {
                "urls": sorted(pushed),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_today_articles() -> list[dict]:
    """取当天新入库且未推送过的文章，按 quality_score 降序。"""
    today = datetime.now().strftime("%Y%m%d")
    pushed = _load_pushed_urls()
    articles = []
    skipped = 0
    for path in ARTICLES_DIR.glob(f"{today}-*.json"):
        if path.name == "index.json":
            continue
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = article.get("source_url") or article.get("url") or ""
        if url and url in pushed:
            skipped += 1
            continue
        articles.append(article)
    if skipped:
        print(f"推送去重：{skipped} 篇已推送过，跳过")
    return sorted(
        articles, key=lambda a: a.get("quality_score", 0.0), reverse=True
    )[:MAX_PUSH]


def build_digest(articles: list[dict]) -> str:
    """把多篇整合成单条 markdown(完整版优先,超长降级紧凑摘要版)。"""
    from distribution.formatter import (
        RELEVANCE_BADGES,
        _relevance_level,
        _relevance_score,
    )

    date = datetime.now().strftime("%Y%m%d")
    heading = f"📰 AI 知识简报 · {date}\n共收录 {len(articles)} 篇(按质量分降序)"

    full = "\n\n---\n\n".join([heading] + [json_to_markdown(a) for a in articles])
    if len(full) <= MAX_DIGEST_CHARS:
        return full

    # 紧凑摘要版:标题 + 一句话 + 链接
    compact = [heading]
    for i, a in enumerate(articles, start=1):
        score = _relevance_score(a)
        badge = RELEVANCE_BADGES[_relevance_level(score)]
        abstract = str(a.get("abstract") or "").strip() or (
            (str(a.get("summary", "")) or "")[:60] + "…"
        )
        compact.append(
            f"{i}. **{a.get('title', '')}** {badge}\n"
            f"> {abstract}\n"
            f"🔗 [阅读原文]({a.get('source_url', '')})"
        )
    return "\n\n".join(compact)


def write_group_payload(articles: list[dict]) -> dict:
    """生成单条整合 digest + manifest，返回推送条数。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.md"):
        old.unlink()
    (OUT_DIR / "manifest.json").unlink(missing_ok=True)

    manifest = {
        "date": datetime.now().strftime("%Y%m%d"),
        "count": len(articles),
        "titles": [a.get("title", "") for a in articles],
    }
    digest = build_digest(articles)
    (OUT_DIR / "digest.md").write_text(digest, encoding="utf-8")
    manifest["digest_chars"] = len(digest)
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if articles:
        _record_pushed(articles)
    return manifest


def format_cost_report(report: dict) -> str:
    """把成本报告 JSON 格式化为私聊纯文本。"""
    lines = [
        "💰 知识管线成本报告",
        f"生成时间：{report.get('generated_at', '')}",
        "",
        f"总成本：{report.get('total_cost_yuan', 0):.6f} 元 / "
        f"预算 {report.get('budget_yuan', 0):.2f} 元"
        f"（已用 {report.get('usage_ratio', 0):.0%}）",
        f"LLM 调用：{report.get('total_calls', 0)} 次",
        f"tokens：prompt {report.get('total_prompt_tokens', 0)} + "
        f"completion {report.get('total_completion_tokens', 0)}",
    ]
    by_node = sorted(
        report.get("by_node", {}).items(),
        key=lambda kv: kv[1].get("cost_yuan", 0),
        reverse=True,
    )
    if by_node:
        lines.append("")
        lines.append("按节点：")
        for node, stats in by_node:
            lines.append(
                f"  - {node}：{stats.get('calls', 0)} 次、"
                f"tokens={stats.get('prompt_tokens', 0)}+{stats.get('completion_tokens', 0)}、"
                f"成本 {stats.get('cost_yuan', 0):.6f} 元"
                f"（占 {stats.get('cost_percent', 0):.1f}%）"
            )
    return "\n".join(lines)


def latest_cost_report() -> Path | None:
    reports = sorted(ROOT_DIR.glob("knowledge/cost-report-*.json"))
    return reports[-1] if reports else None


def main() -> int:
    articles = load_today_articles()
    manifest = write_group_payload(articles)
    print(f"群推送准备：{manifest['count']} 条 → 整合为 1 条"
          f"（{manifest.get('digest_chars', 0)} 字符）")

    cost_path = latest_cost_report()
    if cost_path:
        report = json.loads(cost_path.read_text(encoding="utf-8"))
        COST_OUT.write_text(format_cost_report(report), encoding="utf-8")
        print(f"成本报告：{cost_path.name}")
    else:
        COST_OUT.write_text("💰 本次运行无成本报告", encoding="utf-8")
        print("成本报告：无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
