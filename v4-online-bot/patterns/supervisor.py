"""Supervisor 监督模式：Worker 产出 + Supervisor 审核的质量闭环。

核心思路：单个 Agent 的输出质量不稳定，引入第二个 Agent 做质检，
形成"产出 → 审核 → 反馈重做"的循环，用额外的 token 换取质量下限：

1. Worker Agent：接收任务，输出 JSON 格式的分析报告；
2. Supervisor Agent：从准确性 / 深度 / 格式三个维度打分（各 1-10），
   输出 ``{"passed": bool, "score": int, "feedback": str}``；
3. 审核循环：通过（score >= 7）即返回；不通过则带上反馈让 Worker
   重做，最多 ``max_retries`` 轮；仍不通过则强制返回最后一版并附警告。

Example:
    >>> from patterns.supervisor import supervisor
    >>> result = supervisor("分析 RAG 技术的优缺点")
    >>> print(result["output"], result["final_score"])
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from workflows.model_client import chat  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PASS_SCORE = 7  # 综合分达到该阈值即视为审核通过
DEFAULT_MAX_RETRIES = 3

WORKER_SYSTEM_PROMPT = """你是一名资深分析师（Worker）。针对用户给出的任务，输出一份 JSON 格式的分析报告。

只返回 JSON 对象，不要输出其他内容，格式：
{
  "topic": "任务主题",
  "summary": "一段话的总体概述",
  "key_points": ["要点1", "要点2", "要点3"],
  "conclusion": "结论与建议"
}

要求：内容准确、分析有深度、严格遵守上述 JSON 格式。
如果收到审核反馈，必须针对反馈逐条改进后重新输出完整报告。"""

SUPERVISOR_SYSTEM_PROMPT = """你是一名严格的质量审核员（Supervisor）。请审核 Worker 针对任务产出的分析报告。

评分维度（各 1-10 分）：
- 准确性：内容是否正确、无事实错误、切合任务；
- 深度：分析是否深入、有洞察，而非泛泛而谈；
- 格式：是否为合法 JSON，且包含 topic / summary / key_points / conclusion 字段。

综合分 score 取三个维度的平均分（四舍五入取整）。
score >= 7 时 passed 为 true，否则为 false。
feedback 需具体指出问题与改进方向，通过时可简述优点。

只返回 JSON 对象，不要输出其他内容，格式：
{"passed": true, "score": 8, "feedback": "..."}"""


# ---------------------------------------------------------------------------
# JSON 解析辅助
# ---------------------------------------------------------------------------

_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_json(text: str) -> dict[str, Any] | None:
    """尽力从模型输出中解析 JSON 对象。

    兼容纯 JSON 文本与被 Markdown 代码块包裹的 JSON。

    Args:
        text: 模型返回的原始文本。

    Returns:
        解析后的字典；无法解析或不是对象时返回 None。
    """
    cleaned = _JSON_FENCE_PATTERN.sub("", text.strip()).strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


# ---------------------------------------------------------------------------
# Worker Agent
# ---------------------------------------------------------------------------


def run_worker(task: str, feedback: str | None = None) -> str:
    """Worker Agent：执行任务，输出 JSON 格式的分析报告。

    Args:
        task: 任务描述。
        feedback: 上一轮 Supervisor 的审核反馈；首轮为 None。

    Returns:
        模型输出的报告原文（预期为 JSON 文本）。
    """
    prompt = f"任务：{task}"
    if feedback:
        prompt += (
            f"\n\n上一版报告未通过审核，审核反馈如下：\n{feedback}\n"
            "请针对反馈改进后重新输出完整的 JSON 报告。"
        )

    text, usage = chat(prompt, system_prompt=WORKER_SYSTEM_PROMPT, temperature=0.7)
    logger.info("Worker 产出完成：tokens=%d", usage.total_tokens)
    return text.strip()


# ---------------------------------------------------------------------------
# Supervisor Agent
# ---------------------------------------------------------------------------


def run_supervisor(task: str, output: str) -> dict[str, Any]:
    """Supervisor Agent：审核 Worker 的输出并给出评分与反馈。

    审核结果解析失败或字段缺失时降级为不通过（score=0），
    让循环继续走重做分支，避免劣质输出被误放行。

    Args:
        task: 原始任务描述。
        output: Worker 输出的报告原文。

    Returns:
        ``{"passed": bool, "score": int, "feedback": str}`` 字典。
    """
    prompt = f"任务：{task}\n\nWorker 的报告：\n{output}"
    try:
        text, usage = chat(
            prompt, system_prompt=SUPERVISOR_SYSTEM_PROMPT, temperature=0.0
        )
    except Exception as exc:  # noqa: BLE001 审核失败不应中断主流程
        logger.error("Supervisor 调用失败：%s", exc)
        return {"passed": False, "score": 0, "feedback": f"审核调用失败：{exc}"}

    review = _parse_json(text)
    if review is None:
        logger.warning("Supervisor 输出无法解析为 JSON：%r", text[:200])
        return {"passed": False, "score": 0, "feedback": "审核结果解析失败，请重做。"}

    try:
        score = int(review.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    passed = bool(review.get("passed", False)) and score >= PASS_SCORE
    feedback = str(review.get("feedback", "")).strip() or "无具体反馈。"

    logger.info(
        "Supervisor 审核完成：passed=%s score=%d tokens=%d",
        passed,
        score,
        usage.total_tokens,
    )
    return {"passed": passed, "score": score, "feedback": feedback}


# ---------------------------------------------------------------------------
# 审核循环（统一入口）
# ---------------------------------------------------------------------------


def supervisor(task: str, max_retries: int = DEFAULT_MAX_RETRIES) -> dict[str, Any]:
    """监督模式统一入口：Worker 产出 → Supervisor 审核 → 反馈重做。

    Args:
        task: 任务描述。
        max_retries: 最多允许的产出轮数（含首轮），默认 3。

    Returns:
        结果字典，包含以下字段：

        - ``output``：最终报告文本（JSON 字符串）；
        - ``attempts``：实际产出轮数；
        - ``final_score``：最后一轮的审核综合分；
        - ``warning``：仅在超过重试上限仍未通过时存在，说明质量风险。
    """
    task = task.strip()
    if not task:
        return {"output": "", "attempts": 0, "final_score": 0, "warning": "任务为空。"}

    output = ""
    feedback: str | None = None
    final_score = 0

    for attempt in range(1, max_retries + 1):
        logger.info("第 %d/%d 轮：Worker 开始产出", attempt, max_retries)
        output = run_worker(task, feedback=feedback)

        review = run_supervisor(task, output)
        final_score = review["score"]

        if review["passed"]:
            logger.info("第 %d 轮审核通过：score=%d", attempt, final_score)
            return {
                "output": output,
                "attempts": attempt,
                "final_score": final_score,
            }

        feedback = review["feedback"]
        logger.warning(
            "第 %d 轮审核未通过：score=%d feedback=%s", attempt, final_score, feedback
        )

    # 超过重试上限：强制返回最后一版并附警告
    warning = (
        f"经过 {max_retries} 轮仍未通过审核（最终得分 {final_score}），"
        "强制返回最后一版结果，请人工复核。"
    )
    logger.warning(warning)
    return {
        "output": output,
        "attempts": max_retries,
        "final_score": final_score,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    test_task = "分析 RAG（检索增强生成）技术的优势与局限性"
    print(f"\n{'=' * 60}\n任务：{test_task}\n{'-' * 60}")

    result = supervisor(test_task)
    print(f"产出轮数：{result['attempts']}")
    print(f"最终得分：{result['final_score']}")
    if "warning" in result:
        print(f"⚠️  警告：{result['warning']}")
    print(f"最终报告：\n{result['output']}")
