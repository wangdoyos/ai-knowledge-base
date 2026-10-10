"""多 Agent 流水线的预算守卫（三重保护机制）。

在多 Agent 系统中，审核循环、并行采集都会放大 LLM 调用次数，
成本容易失控。本模块提供三重保护：

1. **成本追踪**：:meth:`CostGuard.record` 记录每次 LLM 调用的
   token 用量，按输入/输出单价换算成人民币成本；
2. **预算熔断**：:meth:`CostGuard.check` 检查累计成本，接近预算
   （>= alert_threshold）时返回 warning，超出预算时抛出
   :class:`BudgetExceededError`，调用方可借此中断工作流；
3. **成本报告**：:meth:`CostGuard.get_report` 按节点分组统计
   成本占比，:meth:`CostGuard.save_report` 落盘 JSON 供事后审计。

usage 字典格式与 :func:`workflows.model_client.chat` 返回的
``Usage`` 对应：``{"prompt_tokens": int, "completion_tokens": int}``。

Example:
    >>> guard = CostGuard(budget_yuan=1.0)
    >>> guard.record("analyzer", {"prompt_tokens": 1000, "completion_tokens": 500})
    >>> status = guard.check()
    >>> status["status"]
    'ok'
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认报告输出目录（相对本文件所在的 v3-multi-agent 根目录）
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "knowledge"


class BudgetExceededError(Exception):
    """累计成本超出预算时抛出，用于中断工作流。"""


@dataclass(frozen=True)
class CostRecord:
    """单次 LLM 调用的成本记录。

    Attributes:
        timestamp: 调用发生时间，格式 ``YYYY-MM-DD HH:MM:SS``。
        node_name: 发起调用的节点名（如 ``planner`` / ``analyzer``）。
        prompt_tokens: 输入（prompt）消耗的 token 数。
        completion_tokens: 输出（completion）消耗的 token 数。
        cost_yuan: 本次调用的成本（人民币元）。
        model: 使用的模型名，空字符串表示未指定。
    """

    timestamp: str
    node_name: str
    prompt_tokens: int
    completion_tokens: int
    cost_yuan: float
    model: str = ""


@dataclass
class CostGuard:
    """预算守卫：成本追踪 + 预算熔断 + 成本报告。

    Attributes:
        budget_yuan: 预算上限（人民币元）。
        alert_threshold: 预警阈值（0~1），用量占比达到该值时告警。
        input_price_per_million: 输入 token 单价（元/百万 token）。
        output_price_per_million: 输出 token 单价（元/百万 token）。
        records: 按时间顺序保存的所有调用记录。
    """

    budget_yuan: float = 1.0
    alert_threshold: float = 0.8
    input_price_per_million: float = 1.0
    output_price_per_million: float = 2.0
    records: list[CostRecord] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 第一重：成本追踪
    # ------------------------------------------------------------------

    def record(
        self,
        node_name: str,
        usage: dict[str, int],
        model: str = "",
    ) -> CostRecord:
        """记录一次 LLM 调用的 token 用量并换算成本。

        Args:
            node_name: 发起调用的节点名。
            usage: 用量字典，格式
                ``{"prompt_tokens": int, "completion_tokens": int}``，
                缺失字段按 0 处理。
            model: 使用的模型名，可选。

        Returns:
            本次生成的 :class:`CostRecord`。
        """
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        cost = (
            prompt_tokens / 1_000_000 * self.input_price_per_million
            + completion_tokens / 1_000_000 * self.output_price_per_million
        )
        entry = CostRecord(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            node_name=node_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_yuan=cost,
            model=model,
        )
        self.records.append(entry)
        logger.debug(
            "记录调用：node=%s tokens=%d+%d cost=%.6f 元",
            node_name,
            prompt_tokens,
            completion_tokens,
            cost,
        )
        return entry

    @property
    def total_prompt_tokens(self) -> int:
        """累计输入 token 数。"""
        return sum(r.prompt_tokens for r in self.records)

    @property
    def total_completion_tokens(self) -> int:
        """累计输出 token 数。"""
        return sum(r.completion_tokens for r in self.records)

    @property
    def total_cost_yuan(self) -> float:
        """累计成本（人民币元）。"""
        return sum(r.cost_yuan for r in self.records)

    # ------------------------------------------------------------------
    # 第二重：预算熔断
    # ------------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """检查当前预算状态。

        Returns:
            状态字典，格式::

                {"status": "ok" | "warning" | "exceeded",
                 "total_cost": float, "budget": float,
                 "usage_ratio": float, "message": str}

        Raises:
            BudgetExceededError: 累计成本超出 ``budget_yuan`` 时抛出，
                异常消息中包含状态字典的 JSON 序列化结果。
        """
        total = self.total_cost_yuan
        ratio = total / self.budget_yuan if self.budget_yuan > 0 else float("inf")

        if total > self.budget_yuan:
            status = {
                "status": "exceeded",
                "total_cost": total,
                "budget": self.budget_yuan,
                "usage_ratio": ratio,
                "message": (
                    f"成本 {total:.6f} 元已超出预算 {self.budget_yuan:.2f} 元，"
                    "工作流应立即中断"
                ),
            }
            logger.error("预算熔断：%s", status["message"])
            raise BudgetExceededError(json.dumps(status, ensure_ascii=False))

        if ratio >= self.alert_threshold:
            status = "warning"
            message = (
                f"成本 {total:.6f} 元已达预算的 {ratio:.0%}"
                f"（预警阈值 {self.alert_threshold:.0%}），请注意控制调用"
            )
            logger.warning("预算预警：%s", message)
        else:
            status = "ok"
            message = f"成本 {total:.6f} 元，预算充足（已用 {ratio:.0%}）"

        return {
            "status": status,
            "total_cost": total,
            "budget": self.budget_yuan,
            "usage_ratio": ratio,
            "message": message,
        }

    # ------------------------------------------------------------------
    # 第三重：成本报告
    # ------------------------------------------------------------------

    def get_report(self) -> dict[str, Any]:
        """生成按节点分组的成本报告。

        Returns:
            报告字典，格式::

                {"generated_at": str, "budget_yuan": float,
                 "total_calls": int, "total_prompt_tokens": int,
                 "total_completion_tokens": int, "total_cost_yuan": float,
                 "usage_ratio": float,
                 "by_node": {node_name: {"calls": int, "prompt_tokens": int,
                                         "completion_tokens": int,
                                         "cost_yuan": float,
                                         "cost_percent": float}},
                 "records": [CostRecord 字典, ...]}
        """
        total_cost = self.total_cost_yuan
        by_node: dict[str, dict[str, Any]] = {}
        for r in self.records:
            stats = by_node.setdefault(
                r.node_name,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cost_yuan": 0.0,
                    "cost_percent": 0.0,
                },
            )
            stats["calls"] += 1
            stats["prompt_tokens"] += r.prompt_tokens
            stats["completion_tokens"] += r.completion_tokens
            stats["cost_yuan"] += r.cost_yuan

        for stats in by_node.values():
            stats["cost_percent"] = (
                stats["cost_yuan"] / total_cost * 100 if total_cost > 0 else 0.0
            )

        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "budget_yuan": self.budget_yuan,
            "total_calls": len(self.records),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cost_yuan": total_cost,
            "usage_ratio": (
                total_cost / self.budget_yuan if self.budget_yuan > 0 else 0.0
            ),
            "by_node": by_node,
            "records": [asdict(r) for r in self.records],
        }

    def save_report(self, path: str | Path | None = None) -> Path:
        """把成本报告保存为 JSON 文件。

        Args:
            path: 输出文件路径。None 时写入
                ``knowledge/cost-report-YYYYMMDD-HHMMSS.json``。

        Returns:
            实际写入的文件路径。
        """
        if path is None:
            filename = time.strftime("cost-report-%Y%m%d-%H%M%S.json")
            path = DEFAULT_REPORT_DIR / filename
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.get_report(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("成本报告已保存：%s", path)
        return path


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. 成本追踪正确性
    guard = CostGuard(budget_yuan=1.0, alert_threshold=0.8)
    guard.record("planner", {"prompt_tokens": 1000, "completion_tokens": 500}, model="deepseek-chat")
    guard.record("analyzer", {"prompt_tokens": 2000, "completion_tokens": 1000}, model="deepseek-chat")
    assert guard.total_prompt_tokens == 3000, guard.total_prompt_tokens
    # 3000/1M*1 + 1500/1M*2 = 0.003 + 0.003 = 0.006 元
    assert abs(guard.total_cost_yuan - 0.006) < 1e-9, guard.total_cost_yuan
    status = guard.check()
    assert status["status"] == "ok", status
    logger.info("测试 1 通过：成本追踪正确，%s", status["message"])

    # 2. 预警阈值触发（0.8 <= 用量占比 < 1.0）
    warn_guard = CostGuard(budget_yuan=1.0, alert_threshold=0.8)
    # 900_000 输入 token -> 0.9 元，占比 90%
    warn_guard.record("analyzer", {"prompt_tokens": 900_000, "completion_tokens": 0})
    status = warn_guard.check()
    assert status["status"] == "warning", status
    assert status["usage_ratio"] >= 0.8, status
    logger.info("测试 2 通过：预警阈值触发，%s", status["message"])

    # 3. 预算超限检测（check 抛出 BudgetExceededError）
    over_guard = CostGuard(budget_yuan=1.0)
    # 600_000 输出 token -> 1.2 元，超出 1.0 元预算
    over_guard.record("reviewer", {"prompt_tokens": 0, "completion_tokens": 600_000})
    try:
        over_guard.check()
    except BudgetExceededError as exc:
        logger.info("测试 3 通过：预算超限已熔断，%s", exc)
    else:
        raise AssertionError("超出预算时 check() 应抛出 BudgetExceededError")

    # 4. 成本报告：按节点分组统计 + 落盘
    report = guard.get_report()
    assert report["total_calls"] == 2, report
    assert set(report["by_node"]) == {"planner", "analyzer"}, report["by_node"]
    assert report["by_node"]["planner"]["calls"] == 1, report["by_node"]
    with tempfile.TemporaryDirectory() as tmp_dir:
        saved = guard.save_report(Path(tmp_dir) / "cost-report.json")
        loaded = json.loads(saved.read_text(encoding="utf-8"))
        assert loaded["total_prompt_tokens"] == 3000, loaded
    logger.info("测试 4 通过：成本报告分组统计与落盘正确")

    logger.info("全部测试通过 ✔")
