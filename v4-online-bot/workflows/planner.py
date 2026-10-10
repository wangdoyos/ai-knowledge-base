"""Planner 智能体节点：根据目标采集量制定三档采集策略。

Planner 位于数据流最上游（plan → sources → ...），是纯本地规则
节点，不调用 LLM：根据目标采集量在 采集广度 / 质量阈值 / 审核轮数
之间做权衡，输出策略 plan 写入状态，供下游采集与审核节点消费。

三档策略：

======== ============== ================ ==================== ===============
档位     target_count   per_source_limit relevance_threshold  max_iterations
======== ============== ================ ==================== ===============
lite     < 10           5                0.7                  1
standard 10 ~ 19        10               0.5                  2
full     >= 20          20               0.4                  3
======== ============== ================ ==================== ===============

Example:
    >>> from workflows.planner import plan_strategy
    >>> plan_strategy(5)["mode"]
    'lite'
    >>> plan_strategy(25)["per_source_limit"]
    20
"""

from __future__ import annotations

import logging
import os
import sys
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

# 目标采集量的环境变量名与默认值（默认落在 standard 档）
ENV_PLANNER_TARGET_COUNT = "PLANNER_TARGET_COUNT"
DEFAULT_TARGET_COUNT = 5

# 三档策略的档位边界
LITE_MAX_TARGET = 10   # target < 10 -> lite
STANDARD_MAX_TARGET = 20  # 10 <= target < 20 -> standard；>= 20 -> full

# 各档位的策略参数与选型理由
STRATEGIES: dict[str, dict[str, Any]] = {
    "lite": {
        "per_source_limit": 5,
        "relevance_threshold": 0.7,
        "max_iterations": 1,
        "rationale": (
            "目标量小（<10），少采精筛：单源限量 5 条控采集成本，"
            "相关性阈值提到 0.7 只留高质量条目，审核 1 轮即可，"
            "不值得为小批量反复返工。"
        ),
    },
    "standard": {
        "per_source_limit": 10,
        "relevance_threshold": 0.5,
        "max_iterations": 2,
        "rationale": (
            "目标量中等（10~19），质量与成本均衡：单源 10 条保证候选池，"
            "阈值 0.5 兼顾召回与质量，审核 2 轮给一次返工机会。"
        ),
    },
    "full": {
        "per_source_limit": 20,
        "relevance_threshold": 0.4,
        "max_iterations": 3,
        "rationale": (
            "目标量大（>=20），扩大召回优先：单源 20 条铺开采集，"
            "阈值放宽到 0.4 避免候选不足，审核 3 轮用多轮返工"
            "把放宽阈值带来的质量风险补回来。"
        ),
    },
}


# ---------------------------------------------------------------------------
# 策略函数
# ---------------------------------------------------------------------------


def plan_strategy(target_count: int | None = None) -> dict[str, Any]:
    """根据目标采集量选择采集策略。

    Args:
        target_count: 目标采集条数；None 时从环境变量
            :data:`ENV_PLANNER_TARGET_COUNT` 读取，
            未配置或非法时用 :data:`DEFAULT_TARGET_COUNT`。

    Returns:
        策略 dict，格式：
        ``{"mode": "lite" | "standard" | "full", "target_count": int,
        "per_source_limit": int, "relevance_threshold": float,
        "max_iterations": int, "rationale": str}``。
    """
    if target_count is None:
        raw = os.environ.get(ENV_PLANNER_TARGET_COUNT, "")
        try:
            target_count = int(raw) if raw else DEFAULT_TARGET_COUNT
        except ValueError:
            logger.warning(
                "环境变量 %s=%r 不是整数，回退默认值 %d",
                ENV_PLANNER_TARGET_COUNT,
                raw,
                DEFAULT_TARGET_COUNT,
            )
            target_count = DEFAULT_TARGET_COUNT

    if target_count < LITE_MAX_TARGET:
        mode = "lite"
    elif target_count < STANDARD_MAX_TARGET:
        mode = "standard"
    else:
        mode = "full"

    return {"mode": mode, "target_count": target_count, **STRATEGIES[mode]}


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------


def planner_node(state: KBState) -> dict[str, Any]:
    """Planner 节点：制定采集策略并写入状态。

    纯本地规则，不调用 LLM、不产生副作用；state 中已有 plan 的
    ``target_count`` 时优先使用（便于调用方通过初始状态定制），
    否则走环境变量 / 默认值。

    Args:
        state: 读取 ``plan``（可选的 ``target_count`` 预设）。

    Returns:
        ``{"plan": {...}}``。
    """
    preset = state["plan"].get("target_count")
    plan = plan_strategy(int(preset) if preset is not None else None)
    print(
        f"[Planner] 策略={plan['mode']} target={plan['target_count']}："
        f"单源限量 {plan['per_source_limit']}、"
        f"阈值 {plan['relevance_threshold']}、"
        f"审核上限 {plan['max_iterations']} 轮"
    )
    print(f"[Planner] 理由：{plan['rationale']}")
    return {"plan": plan}


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from workflows.state import make_initial_state

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. 三档边界测试
    for count, expected in [(5, "lite"), (9, "lite"), (10, "standard"),
                            (19, "standard"), (20, "full"), (50, "full")]:
        plan = plan_strategy(count)
        assert plan["mode"] == expected, f"target={count} 应为 {expected}，实际 {plan['mode']}"
        logger.info("target=%d -> %s（%s）", count, plan["mode"], plan["rationale"][:30])

    # 2. 环境变量与默认值测试
    os.environ.pop(ENV_PLANNER_TARGET_COUNT, None)
    assert plan_strategy()["target_count"] == DEFAULT_TARGET_COUNT
    os.environ[ENV_PLANNER_TARGET_COUNT] = "25"
    assert plan_strategy()["mode"] == "full"
    os.environ[ENV_PLANNER_TARGET_COUNT] = "abc"
    assert plan_strategy()["target_count"] == DEFAULT_TARGET_COUNT
    os.environ.pop(ENV_PLANNER_TARGET_COUNT, None)
    logger.info("环境变量测试通过")

    # 3. 节点包装测试：初始状态 / 预设 target_count
    update = planner_node(make_initial_state())
    assert update["plan"]["mode"] == "standard"
    state = make_initial_state()
    state["plan"] = {"target_count": 30}
    update = planner_node(state)
    assert update["plan"]["mode"] == "full"
    logger.info("节点包装测试通过")
