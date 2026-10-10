"""Collector 智能体节点：采集 GitHub 仓库与 RSS 源的 AI 相关内容。

数据流最上游的采集环节（plan → sources）：单源采集限量优先取
plan 的 ``per_source_limit``（Planner 三档策略），无显式计划时用
默认值。采集渠道有两类：

- GitHub Search API：AI/LLM/Agent 相关的高星开源仓库；
- RSS/Atom 源：配置见 ``workflows/rss_sources.yaml``（与
  v2-automation/pipeline/rss_sources.yaml 同格式），每次运行动态
  加载，仅采集 ``enabled: true`` 的源，新增/启停源无需改代码。

完整原始响应落盘 ``knowledge/raw/``，状态里只保留清洗后
的来源摘要，遵循报告式通信原则。

安全接入：外部内容（标题/简介）在进入状态前经
:func:`tests.security.secure_input` 防护（限流 + 注入检测 +
控制字符清除 + 审计），防止恶意内容污染下游 LLM 提示词。

Example:
    >>> from workflows.collector import collect_node
    >>> from workflows.state import make_initial_state
    >>> update = collect_node(make_initial_state())
    >>> update.keys()
    dict_keys(['sources'])
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import yaml

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tests.security import secure_input  # noqa: E402
from workflows.state import KBState  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

GITHUB_SEARCH_API = "https://api.github.com/search/repositories"
HTTP_TIMEOUT_SECONDS = 10
# 瞬时网络故障重试次数与递增退避基数（与 v2 参考实现对齐）
HTTP_FETCH_RETRIES = 3
HTTP_RETRY_BACKOFF_SECONDS = 2
# 无显式计划时的默认单源采集限量；plan.per_source_limit 优先
GITHUB_RESULT_LIMIT = 8
# 采集主题：面向 AI/LLM/Agent 相关的开源仓库
GITHUB_QUERY = "AI LLM agent in:name,description"
# 统一 User-Agent（Reddit 等 RSS 源要求自定义 UA，否则可能被限流 429）
HTTP_USER_AGENT = "ai-knowledge-base-workflow"

# RSS 源配置文件（与 v2-automation/pipeline/rss_sources.yaml 同格式）
RSS_SOURCES_CONFIG = Path(__file__).resolve().parent / "rss_sources.yaml"

# 原始响应落盘目录（完整 API/feed 响应不进状态）
RAW_DIR = ROOT_DIR / "knowledge" / "raw"

# 知识库文章目录（采集去重的已知 URL 来源）
ARTICLES_DIR = ROOT_DIR / "knowledge" / "articles"

# RSS 2.0 的 <item> 与 Atom 的 <entry> 两种条目块（Reddit/HuggingFace
# 等源是 Atom 格式，两种都要能解析）
_RSS_ITEM_RE = re.compile(r"<item[^>]*>(.*?)</item>", re.DOTALL)
_ATOM_ENTRY_RE = re.compile(r"<entry[^>]*>(.*?)</entry>", re.DOTALL)
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.DOTALL)
# Atom 的链接在 <link href="..."/> 属性里，与 RSS 的 <link>文本</link> 不同
_ATOM_LINK_RE = re.compile(r'<link[^>]*?href="([^"]+)"')
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# RSS 解析辅助
# ---------------------------------------------------------------------------


def _xml_tag(block: str, tag: str) -> str:
    """从条目块中提取指定标签的文本内容。

    Args:
        block: 单个 ``<item>`` / ``<entry>`` 内部的 XML 文本。
        tag: 标签名，如 ``title``、``dc:creator``。

    Returns:
        去除 CDATA 与 HTML 实体后的文本，未找到时返回空字符串。
    """
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL)
    if not match:
        return ""
    text = match.group(1).strip()
    cdata = _CDATA_RE.match(text)
    if cdata:
        text = cdata.group(1).strip()
    return html.unescape(text)


def _to_iso8601(text: str) -> str:
    """把 RSS 的 RFC 822 时间统一为 ISO 8601 字符串。

    Atom 源的 ``<published>`` / ``<updated>`` 本身就是 ISO 8601，
    解析失败时原样返回，由下游 Organizer 兜底。

    Args:
        text: 时间文本，如 ``Wed, 29 Jul 2026 22:51:03 +0000``。

    Returns:
        ISO 8601 字符串；空输入或解析失败时返回原值。
    """
    if not text:
        return ""
    try:
        return parsedate_to_datetime(text).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return text


def _parse_feed_entries(text: str) -> list[dict[str, str]]:
    """解析 feed 文本，兼容 RSS 2.0 与 Atom 两种格式。

    Args:
        text: feed 的原始 XML 文本。

    Returns:
        条目字典列表，键为 title / link / description / author /
        published_at（缺失字段为空字符串）。
    """
    entries: list[dict[str, str]] = []
    item_blocks = _RSS_ITEM_RE.findall(text)
    if item_blocks:
        # RSS 2.0：<item> + <link>文本</link> + pubDate（RFC 822）
        for block in item_blocks:
            entries.append(
                {
                    "title": _xml_tag(block, "title"),
                    "link": _xml_tag(block, "link"),
                    "description": _xml_tag(block, "description"),
                    "author": _xml_tag(block, "dc:creator")
                    or _xml_tag(block, "author"),
                    "published_at": _to_iso8601(_xml_tag(block, "pubDate")),
                }
            )
        return entries

    # Atom：<entry> + <link href="..."/> + published/updated（ISO 8601）
    for block in _ATOM_ENTRY_RE.findall(text):
        link_match = _ATOM_LINK_RE.search(block)
        author_block = re.search(r"<author>(.*?)</author>", block, re.DOTALL)
        entries.append(
            {
                "title": _xml_tag(block, "title"),
                "link": html.unescape(link_match.group(1)) if link_match else "",
                "description": _xml_tag(block, "summary")
                or _xml_tag(block, "content"),
                "author": _xml_tag(author_block.group(1), "name")
                if author_block
                else "",
                "published_at": _xml_tag(block, "published")
                or _xml_tag(block, "updated"),
            }
        )
    return entries


def load_rss_sources(config_path: Path = RSS_SOURCES_CONFIG) -> list[dict[str, str]]:
    """从 YAML 配置文件加载已启用的 RSS 数据源列表。

    每次调用都重新读取配置文件，新增/启停数据源无需改代码即可生效。
    配置异常不抛错（采集节点失败时下游自然短路），记录日志后返回空列表。

    Args:
        config_path: 配置文件路径，默认 workflows/rss_sources.yaml。

    Returns:
        已启用源的列表，每项含 name / url / category。
    """
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error("RSS 配置文件读取失败：%s（%s）", config_path, exc)
        return []
    except yaml.YAMLError as exc:
        logger.error("RSS 配置文件不是合法 YAML：%s（%s）", config_path, exc)
        return []

    entries = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        logger.error("RSS 配置缺少 sources 列表：%s", config_path)
        return []

    sources: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
            logger.warning("跳过缺少 name/url 的 RSS 源配置：%r", entry)
            continue
        if not entry.get("enabled", False):
            continue
        sources.append(
            {
                "name": str(entry["name"]),
                "url": str(entry["url"]),
                "category": str(entry.get("category", "")),
            }
        )
    return sources


def _fetch_text(url: str, headers: dict[str, str]) -> str | None:
    """带重试的 HTTP GET，返回响应文本。

    瞬时网络故障（连接重置、超时等）最多重试 HTTP_FETCH_RETRIES 次，
    重试间隔按 HTTP_RETRY_BACKOFF_SECONDS 递增退避。

    Args:
        url: 请求地址。
        headers: 请求头。

    Returns:
        响应文本；全部尝试失败时记录日志返回 None。
    """
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, HTTP_FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(
                request, timeout=HTTP_TIMEOUT_SECONDS
            ) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            logger.warning(
                "请求失败（第 %d/%d 次）：HTTP %s %s（%s）",
                attempt, HTTP_FETCH_RETRIES, exc.code, exc.reason, url,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "请求失败（第 %d/%d 次）：%s（%s）",
                attempt, HTTP_FETCH_RETRIES, exc, url,
            )
        if attempt < HTTP_FETCH_RETRIES:
            time.sleep(HTTP_RETRY_BACKOFF_SECONDS * attempt)
    logger.error("请求重试耗尽，放弃：%s", url)
    return None


# ---------------------------------------------------------------------------
# 采集实现
# ---------------------------------------------------------------------------


def _secure_source_item(
    title: str, description: str, url: str
) -> tuple[str, str, int] | None:
    """对单条外部内容做安全清洗。

    Args:
        title: 条目标题（不含换行，合并清洗后取首行回拆）。
        description: 条目简介。
        url: 条目链接（仅用于日志）。

    Returns:
        ``(清洗后标题, 清洗后简介, 告警数)``；被限流时返回 None，
        调用方直接丢弃该条，不带脏数据下游。
    """
    raw_text = f"{title}\n{description.strip()}"
    cleaned, warnings = secure_input(raw_text, client_id="collector")
    if not cleaned:
        logger.warning("采集清洗被限流，丢弃条目：%s", url)
        return None
    if warnings:
        logger.warning("采集内容安全告警（%s）：%s", url, "；".join(warnings))
    clean_title, _, clean_desc = cleaned.partition("\n")
    return clean_title, clean_desc, len(warnings)


def load_known_urls() -> set[str]:
    """加载知识库中全部已入库文章的 URL（采集去重用）。

    扫描 ``knowledge/articles/*.json``（跳过 index.json），收集
    ``source_url``（兼容旧条目的 ``url`` 键）。重复 URL 的文章在
    采集阶段直接丢弃，不进 sources，从而不触发下游 LLM 分析。

    Returns:
        已入库 URL 的集合；目录不存在或读取失败时返回空集合。
    """
    known: set[str] = set()
    if not ARTICLES_DIR.is_dir():
        return known
    for path in ARTICLES_DIR.glob("*.json"):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = data.get("source_url") or data.get("url") or ""
        if url:
            known.add(url)
    return known


def _collect_github(limit: int, collected_at: str) -> tuple[list[dict[str, Any]], int]:
    """调用 GitHub Search API 抓取 AI 相关仓库。

    Args:
        limit: 单源采集限量。
        collected_at: 本次采集时间（ISO 8601）。

    Returns:
        ``(来源摘要列表, 安全告警数)``；请求失败时列表为空。
    """
    encoded = urllib.parse.quote(GITHUB_QUERY)
    url = (
        f"{GITHUB_SEARCH_API}?q={encoded}"
        f"&sort=stars&order=desc&per_page={limit}"
    )
    text = _fetch_text(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    if text is None:
        return [], 0
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("GitHub 响应不是合法 JSON：%s", exc)
        return [], 0

    # 完整原始响应落盘，不进入状态
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"github-{datetime.now():%Y%m%d-%H%M%S}.json"
    raw_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sources: list[dict[str, Any]] = []
    security_warnings = 0
    for item in data.get("items", []):
        html_url = item.get("html_url", "")
        if not html_url:
            continue
        cleaned = _secure_source_item(
            item.get("full_name", "未知仓库"),
            item.get("description") or "",
            html_url,
        )
        if cleaned is None:
            continue
        title, description, warnings = cleaned
        security_warnings += warnings
        owner = item.get("owner") or {}
        sources.append(
            {
                "source": "github",
                "title": title or "未知仓库",
                "url": html_url,
                "summary": f"⭐ {item.get('stargazers_count', 0)}｜{description or '无描述'}",
                # 仓库作者与创建时间（ISO 8601），对应知识条目的
                # author / published_at 字段；缺失时由 Organizer 兜底
                "author": str(owner.get("login", "") or ""),
                "published_at": str(item.get("created_at", "") or ""),
                "collected_at": collected_at,
            }
        )
    print(f"[Collect] GitHub 采集完成：{len(sources)} 条，原始响应已落盘 {raw_path.name}")
    return sources, security_warnings


def _collect_rss(limit: int, collected_at: str) -> tuple[list[dict[str, Any]], int]:
    """从 rss_sources.yaml 中启用的 RSS/Atom 源采集内容。

    每个源最多取 ``limit`` 条（与 GitHub 的单源限量对齐）；单个
    源请求或解析失败只记日志跳过，不影响其它源。

    Args:
        limit: 单源采集限量。
        collected_at: 本次采集时间（ISO 8601）。

    Returns:
        ``(来源摘要列表, 安全告警数)``。
    """
    feeds = load_rss_sources()
    if not feeds:
        logger.warning("没有可用的 RSS 源（配置缺失或全部未启用）")
        return [], 0
    print(f"[Collect] RSS 配置加载完成：启用 {len(feeds)} 个源")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    sources: list[dict[str, Any]] = []
    security_warnings = 0
    for feed in feeds:
        text = _fetch_text(feed["url"], headers={"User-Agent": HTTP_USER_AGENT})
        if text is None:
            print(f"[Collect] RSS 源请求失败，跳过：{feed['name']}")
            continue

        # 完整原始 feed 落盘，不进入状态
        raw_path = RAW_DIR / f"rss-{feed['name']}-{timestamp}.xml"
        raw_path.write_text(text, encoding="utf-8")

        entries = _parse_feed_entries(text)
        if not entries:
            logger.warning("RSS 源未解析出任何条目：%s", feed["name"])
            continue

        count = 0
        for entry in entries:
            if count >= limit:
                break
            if not entry["title"] or not entry["link"]:
                logger.warning("跳过缺少 title/link 的 RSS 条目（%s）", feed["name"])
                continue
            # 简介去 HTML 标签并截断，避免长网页片段进状态
            description = _HTML_TAG_RE.sub("", entry["description"]).strip()[:500]
            cleaned = _secure_source_item(entry["title"], description, entry["link"])
            if cleaned is None:
                continue
            title, clean_desc, warnings = cleaned
            security_warnings += warnings
            sources.append(
                {
                    "source": feed["name"],
                    "title": title or "无标题",
                    "url": entry["link"],
                    "summary": f"[{feed['category']}] {clean_desc or '无描述'}",
                    "author": entry["author"],
                    "published_at": entry["published_at"],
                    "collected_at": collected_at,
                }
            )
            count += 1
        print(f"[Collect] RSS 源 {feed['name']} 采集完成：{count} 条")
    return sources, security_warnings


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


def collect_node(state: KBState) -> dict[str, Any]:
    """采集节点：汇总 GitHub 仓库与 RSS 源两类渠道的采集结果。

    单源采集限量优先取 plan 的 ``per_source_limit``（Planner 三档
    策略），无显式计划时用 :data:`GITHUB_RESULT_LIMIT`。
    完整原始响应落盘 ``knowledge/raw/``，状态里只保留清洗后的
    来源摘要（source / title / url / summary / author /
    published_at / collected_at），其中 author 与 published_at
    供 Organizer 组装 AGENTS.md 知识条目时直接复用。

    Args:
        state: 读取 ``plan`` 的 ``per_source_limit``。

    Returns:
        ``{"sources": [...]}``；全部渠道失败时 sources 为空列表，
        由下游节点自然短路。每条外部内容经 secure_input 清洗，
        被限流的条目直接丢弃。
    """
    limit = int(state["plan"].get("per_source_limit", GITHUB_RESULT_LIMIT))
    print(f"[Collect] 开始采集：GitHub + RSS，单源限量 {limit}")

    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    github_sources, github_warnings = _collect_github(limit, collected_at)
    rss_sources, rss_warnings = _collect_rss(limit, collected_at)

    sources = github_sources + rss_sources
    print(
        f"[Collect] 采集完成：共 {len(sources)} 条来源"
        f"（GitHub {len(github_sources)} 条 / RSS {len(rss_sources)} 条，"
        f"安全告警 {github_warnings + rss_warnings} 条）"
    )

    # 按知识库已有 URL 去重：重复文章不进 sources，不调 LLM 分析
    known_urls = load_known_urls()
    if known_urls:
        fresh: list[dict[str, Any]] = []
        skipped = 0
        for source in sources:
            if source.get("url") in known_urls:
                skipped += 1
            else:
                fresh.append(source)
        if skipped:
            print(
                f"[Collect] 去重：{skipped} 条已在知识库中，跳过"
                "（不调 LLM 分析）"
            )
            sources = fresh
    return {"sources": sources}


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from workflows.state import make_initial_state

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 真实调用 GitHub API 与 RSS 源（无需 LLM Key）
    update = collect_node(make_initial_state())
    for source in update["sources"]:
        print(f"  - [{source['source']}] {source['title']}：{source['summary'][:60]}")
