"""HumanFlag 智能体节点：审核循环超限时的人工介入出口（异常终点）。

循环必须有出口。超过审核次数上限仍未通过，说明问题不在"质量"
而在"数据"，需要人工判断。本节点把问题条目连同最后一轮审核反馈
写入独立目录 ``knowledge/pending_review/``，不污染主知识库
（``knowledge/articles/``），并在状态里打上 ``needs_human_review``
标记后结束流程。

Example:
    >>> from workflows.human_flag import human_flag_node
    >>> from workflows.state import make_initial_state
    >>> update = human_flag_node(make_initial_state())
    >>> update["needs_human_review"]
    True
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import workflows 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from workflows.state import KBState  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 待人工复核的条目落盘目录，与主知识库 knowledge/articles/ 隔离
PENDING_DIR = ROOT_DIR / "knowledge" / "pending_review"
# 打印最后一轮审核反馈时的截断长度
FEEDBACK_PREVIEW_CHARS = 200


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


def human_flag_node(state: KBState) -> dict[str, Any]:
    """HumanFlag 节点：审核循环超过上限时的兜底，写入待复核目录。

    把当前 analyses、最后一轮审核反馈与循环次数打包写入
    ``knowledge/pending_review/pending-{时间戳}.json``（UTC 时间戳），
    供人工判断数据本身是否有问题；写盘失败只记日志，
    仍返回人工介入标记，不让兜底节点自己再抛异常。

    Args:
        state: 读取 ``analyses`` / ``iteration`` / ``review_feedback``。

    Returns:
        ``{"needs_human_review": True}``。
    """
    analyses = state["analyses"]
    iteration = state["iteration"]
    feedback = state["review_feedback"]

    print(f"[HumanFlag] 达到 {iteration} 次审核仍未通过，转人工介入")
    print(f"[HumanFlag] 最后反馈: {feedback[:FEEDBACK_PREVIEW_CHARS]}")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    filepath = PENDING_DIR / f"pending-{timestamp}.json"
    payload = {
        "timestamp": timestamp,
        "iterations_used": iteration,
        "last_feedback": feedback,
        "analyses": analyses,
    }
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        filepath.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[HumanFlag] 已保存到 {filepath}")
    except OSError as exc:
        # 兜底节点不应再抛异常：留日志并继续返回人工介入标记
        logger.error("待复核条目写盘失败：%s", exc)

    return {"needs_human_review": True}


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from workflows.state import make_initial_state

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 模拟一份审核 3 轮未通过的状态，验证落盘与返回值
    state = make_initial_state()
    state["analyses"] = [
        {
            "url": "https://github.com/example/repo",
            "category": "其他",
            "tags": ["测试"],
            "key_points": ["仅用于人工介入节点的手动测试"],
            "score": 0.3,
            "summary": "这是一条测试数据，验证 pending_review 落盘。",
        }
    ]
    state["iteration"] = 3
    state["review_feedback"] = "第 1 条 summary 与仓库实际内容不符，疑似源数据有误。"

    update = human_flag_node(state)
    print(f"\n返回值：{update}")
    assert update == {"needs_human_review": True}
