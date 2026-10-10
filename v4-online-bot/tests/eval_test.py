"""AI 知识库分析质量的评估测试（pytest）。

用小规模评估集（EVAL_CASES）验证 LLM 分析的关键行为：

- 正面案例：技术文章输入，预期产出摘要与关键词标签；
- 负面案例：无关内容输入，预期被打低分或归入"其他"类；
- 边界案例：极短输入（如 "AI"），预期不崩溃、返回合法结构。

LLM 输出天然不稳定，因此断言全部使用范围条件（>=、<=、in），
不做精确匹配。真实调用 LLM 的测试标记为 ``slow``，可选跳过：

    pytest tests/eval_test.py                # 全部运行（需 API Key）
    pytest tests/eval_test.py -m "not slow"  # 只跑本地验证，不调 LLM
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

# 自定义 slow 标记未在 pytest 配置中注册，屏蔽 UnknownMark 警告
warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)

# 项目根目录（v3-multi-agent/），保证从任意位置运行 pytest 都能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 加载 .env，让 pytest 进程能读到 LLM_API_KEY 及各提供商的 API Key
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT_DIR / ".env")

from workflows.model_client import (  # noqa: E402
    DEFAULT_PROVIDER,
    ENV_LLM_PROVIDER,
    PROVIDER_CONFIGS,
    LLMError,
    chat,
)

# ---------------------------------------------------------------------------
# API Key 解析：兼容通用的 LLM_API_KEY 与提供商专属变量
# ---------------------------------------------------------------------------

_provider = os.environ.get(ENV_LLM_PROVIDER, DEFAULT_PROVIDER).strip().lower()
_key_env = PROVIDER_CONFIGS.get(_provider, PROVIDER_CONFIGS[DEFAULT_PROVIDER]).api_key_env
# .env 中若只配置了通用的 LLM_API_KEY，则映射到当前提供商的专属变量
if os.environ.get("LLM_API_KEY") and not os.environ.get(_key_env):
    os.environ[_key_env] = os.environ["LLM_API_KEY"]

HAS_API_KEY = bool(os.environ.get(_key_env))

requires_llm = pytest.mark.skipif(
    not HAS_API_KEY,
    reason=f"未配置 LLM_API_KEY / {_key_env}，跳过真实 LLM 调用测试",
)

# ---------------------------------------------------------------------------
# 评估集：每个用例包含 name / input / expected（检查函数 + 范围条件）
# ---------------------------------------------------------------------------

EVAL_CASES: list[dict[str, Any]] = [
    {
        # 正面案例：典型技术文章，预期有摘要、有关键词、评分偏高
        "name": "positive_tech_article",
        "input": (
            "仓库名：langchain-ai/langgraph\n"
            "链接：https://github.com/langchain-ai/langgraph\n"
            "简介：⭐ 10000｜Build resilient language agents as graphs. "
            "用图结构编排多智能体工作流，支持状态管理、条件路由与人工审核。"
        ),
        "expected": {
            "min_summary_chars": 20,     # 摘要至少 20 字
            "min_tags": 1,               # 至少 1 个关键词标签
            "score_range": (0.5, 1.0),   # 高相关内容评分应 >= 0.5
            "check": lambda r: len(r["summary"]) >= 20 and len(r["tags"]) >= 1,
        },
    },
    {
        # 负面案例：与 AI/技术完全无关的内容，预期低分或归入"其他"
        "name": "negative_irrelevant_content",
        "input": (
            "仓库名：今日菜谱分享\n"
            "链接：https://example.com/food\n"
            "简介：红烧肉的家常做法：五花肉焯水后加冰糖炒糖色，"
            "小火慢炖四十分钟，出锅前大火收汁。"
        ),
        "expected": {
            "score_range": (0.0, 0.5),   # 无关内容评分应 <= 0.5
            "check": lambda r: r["score"] <= 0.5 or r["category"] == "其他",
        },
    },
    {
        # 边界案例：极短输入，预期不崩溃且返回合法结构
        "name": "edge_minimal_input",
        "input": "AI",
        "expected": {
            "score_range": (0.0, 1.0),   # 只要求评分落在合法区间
            "check": lambda r: isinstance(r, dict) and 0.0 <= r["score"] <= 1.0,
        },
    },
]

# ---------------------------------------------------------------------------
# 分析辅助：调用 chat() 生成结构化分析结果
# ---------------------------------------------------------------------------

ANALYZE_SYSTEM_PROMPT = """你是一个技术知识库的内容分析师。针对给定的内容，
生成结构化分析结果，只返回 JSON 对象，格式：

{
  "summary": "80~150 字的中文摘要",
  "category": "分类，从以下选择一个：大模型 / 智能体 / 工具框架 / 数据工程 / 其他",
  "tags": ["3~5 个中文或通用英文标签"],
  "score": 0.85
}

score 为 0~1 的价值评分：与 AI/LLM/Agent 主题的相关性越高分越高，
无关内容给低分。不要输出 JSON 以外的任何内容。"""

_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_analysis(text: str) -> dict[str, Any]:
    """把模型输出解析为分析结果字典，并做类型归一化。"""
    cleaned = _JSON_FENCE_PATTERN.sub("", text.strip()).strip()
    raw = json.loads(cleaned)
    assert isinstance(raw, dict), f"分析结果应为 JSON 对象：{cleaned[:200]!r}"
    return {
        "summary": str(raw.get("summary", "")),
        "category": str(raw.get("category", "其他")),
        "tags": [str(t) for t in raw.get("tags", [])],
        "score": float(raw.get("score", 0.0)),
    }


@lru_cache(maxsize=None)
def run_analysis(content: str) -> dict[str, Any]:
    """对输入内容执行一次 LLM 分析（同一输入只调用一次，结果缓存）。"""
    text, _usage = chat(content, system_prompt=ANALYZE_SYSTEM_PROMPT, temperature=0.0)
    return _parse_analysis(text)


# ---------------------------------------------------------------------------
# 本地验证测试：不调用 LLM，验证 EVAL_CASES 自身结构
# ---------------------------------------------------------------------------


def test_eval_cases_structure() -> None:
    """评估集结构合法：字段齐全、类型正确、范围条件自洽。"""
    assert len(EVAL_CASES) >= 3, "评估集至少应覆盖正面/负面/边界 3 种场景"

    names = [case["name"] for case in EVAL_CASES]
    assert len(names) == len(set(names)), f"用例 name 不允许重复：{names}"

    for case in EVAL_CASES:
        # 必需字段齐全
        for field in ("name", "input", "expected"):
            assert field in case, f"用例缺少字段 {field!r}：{case}"

        assert isinstance(case["name"], str) and case["name"]
        assert isinstance(case["input"], str) and case["input"]

        expected = case["expected"]
        assert isinstance(expected, dict)
        # 每个用例至少包含一个检查函数
        assert callable(expected.get("check")), f"{case['name']} 缺少 check 函数"

        # 范围条件自洽：score_range 为 [0,1] 内的合法闭区间
        low, high = expected["score_range"] if "score_range" in expected else (0.0, 1.0)
        assert 0.0 <= low <= high <= 1.0, f"{case['name']} 的 score_range 非法"


# ---------------------------------------------------------------------------
# LLM 评估测试：逐用例分析并做范围断言（slow，可选跳过）
# ---------------------------------------------------------------------------


@pytest.mark.slow
@requires_llm
@pytest.mark.parametrize("case", EVAL_CASES, ids=lambda c: c["name"])
def test_analysis_eval(case: dict[str, Any]) -> None:
    """对每个评估用例执行分析，用范围条件断言输出质量。"""
    try:
        result = run_analysis(case["input"])
    except LLMError as exc:
        pytest.fail(f"{case['name']} 分析调用失败（不应崩溃）：{exc}")

    expected = case["expected"]

    # 结构断言：分类落在候选集合内（范围条件 in）
    assert result["category"] in {"大模型", "智能体", "工具框架", "数据工程", "其他"}, result

    # 范围断言：评分落在期望区间
    low, high = expected.get("score_range", (0.0, 1.0))
    assert low <= result["score"] <= high, (
        f"{case['name']} 评分 {result['score']} 不在期望区间 [{low}, {high}]：{result}"
    )

    # 范围断言：摘要长度与标签数量下限
    if "min_summary_chars" in expected:
        assert len(result["summary"]) >= expected["min_summary_chars"], result
    if "min_tags" in expected:
        assert len(result["tags"]) >= expected["min_tags"], result

    # 用例自带的检查函数
    assert expected["check"](result), f"{case['name']} 的 check 未通过：{result}"


# ---------------------------------------------------------------------------
# LLM-as-Judge 测试：让 LLM 给分析结果打 1~10 分（slow，可选跳过）
# ---------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = (
    "你是一个严格的评审员。根据原始输入与分析结果，评估分析质量："
    "摘要是否准确、标签是否贴切、评分是否合理。"
    "给出 1~10 的整数总分（10 为最好），只输出这个数字，不要输出其他内容。"
)


@pytest.mark.slow
@requires_llm
def test_llm_as_judge() -> None:
    """LLM-as-Judge：对正面案例的分析结果打分，分数应 >= 5。"""
    case = EVAL_CASES[0]  # positive_tech_article
    analysis = run_analysis(case["input"])

    judge_prompt = (
        f"原始输入：\n{case['input']}\n\n"
        f"分析结果：\n{json.dumps(analysis, ensure_ascii=False, indent=2)}\n\n"
        "请给出 1~10 的整数总分。"
    )
    text, _usage = chat(judge_prompt, system_prompt=JUDGE_SYSTEM_PROMPT, temperature=0.0)

    match = re.search(r"\d+", text)
    assert match, f"评审输出中未找到分数：{text!r}"
    score = int(match.group())

    # 范围断言：分数合法且不低于及格线
    assert 1 <= score <= 10, f"分数超出 1~10 范围：{score}（原始输出：{text!r}）"
    assert score >= 5, f"LLM 评审分数过低：{score}（分析结果：{analysis}）"
