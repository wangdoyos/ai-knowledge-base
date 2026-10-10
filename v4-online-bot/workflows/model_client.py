"""统一的 LLM 调用客户端（v3 精简版，零第三方依赖）。

参考 v2 的 ``pipeline/model_client.py``，改用标准库 ``urllib.request``
发起 HTTP 请求，去掉 httpx 依赖。支持 DeepSeek / Qwen / OpenAI 三种
模型提供商，通过环境变量切换：

- ``LLM_PROVIDER``：提供商名称（``deepseek`` / ``qwen`` / ``openai``），默认 ``deepseek``
- ``DEEPSEEK_API_KEY`` / ``QWEN_API_KEY`` / ``OPENAI_API_KEY``：对应提供商的 API Key
- ``LLM_MODEL``：可选，覆盖默认模型名

三家均暴露 OpenAI 兼容的 ``/chat/completions`` 接口，因此统一实现。

对外暴露两个入口：

- :func:`chat`：返回 ``(text, usage)`` 元组；
- :func:`chat_json`：强制 JSON 输出，返回 ``(parsed_json, usage)`` 元组；
- :func:`accumulate_usage`：把单次 usage 累加到状态里的 cost_tracker。

内置预算守卫：每次调用完成后自动把用量记入全局 :class:`CostGuard`
（懒加载单例，预算通过环境变量 ``BUDGET_YUAN`` 配置，默认 1.0 元），
超出预算时抛出 :class:`BudgetExceededError` 中断工作流。

Example:
    >>> from workflows.model_client import chat, chat_json
    >>> text, usage = chat("用一句话介绍 LLM Agent")
    >>> result, usage = chat_json('返回 JSON：{"lang": "用户使用的语言"}')
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 项目根目录（v3-multi-agent/），直接运行本文件时也能 import tests 包
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tests.cost_guard import BudgetExceededError, CostGuard  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS: float = 60.0
MAX_RETRIES: int = 3
RETRY_BASE_DELAY_SECONDS: float = 1.0

ENV_LLM_PROVIDER = "LLM_PROVIDER"
ENV_LLM_MODEL = "LLM_MODEL"
DEFAULT_PROVIDER = "deepseek"

ENV_BUDGET_YUAN = "BUDGET_YUAN"
DEFAULT_BUDGET_YUAN = 1.0


@dataclass(frozen=True)
class ProviderConfig:
    """提供商静态配置。

    Attributes:
        base_url: OpenAI 兼容 API 的根地址（不含 ``/chat/completions``）。
        default_model: 未指定模型时使用的默认模型名。
        api_key_env: 存放 API Key 的环境变量名。
    """

    base_url: str
    default_model: str
    api_key_env: str


PROVIDER_CONFIGS: dict[str, ProviderConfig] = {
    "deepseek": ProviderConfig(
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
    ),
    "qwen": ProviderConfig(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        api_key_env="QWEN_API_KEY",
    ),
    "openai": ProviderConfig(
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
    ),
}


# ---------------------------------------------------------------------------
# 异常与数据模型
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """LLM 调用相关的基础异常。"""


class LLMConfigError(LLMError):
    """配置错误（提供商名非法、缺少 API Key 等）。"""


class LLMRequestError(LLMError):
    """请求失败（网络异常、HTTP 非 2xx、响应格式异常等）。"""


@dataclass(frozen=True)
class Usage:
    """一次调用的 token 用量统计。

    Attributes:
        prompt_tokens: 输入（prompt）消耗的 token 数。
        completion_tokens: 输出（completion）消耗的 token 数。
        total_tokens: 总 token 数。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# 全局预算守卫（懒加载单例）
# ---------------------------------------------------------------------------

_cost_guard: CostGuard | None = None


def get_cost_guard() -> CostGuard:
    """获取全局 CostGuard 实例（懒加载，首次调用时创建）。

    预算上限从环境变量 ``BUDGET_YUAN`` 读取，未配置或非法时使用
    默认值 1.0 元。后续调用始终复用同一实例，保证整个进程内的
    成本累计口径一致。

    Returns:
        全局唯一的 :class:`CostGuard` 实例。
    """
    global _cost_guard
    if _cost_guard is None:
        raw = os.environ.get(ENV_BUDGET_YUAN, "")
        try:
            budget = float(raw) if raw else DEFAULT_BUDGET_YUAN
        except ValueError:
            logger.warning(
                "环境变量 %s=%r 不是合法数字，使用默认预算 %.2f 元",
                ENV_BUDGET_YUAN,
                raw,
                DEFAULT_BUDGET_YUAN,
            )
            budget = DEFAULT_BUDGET_YUAN
        _cost_guard = CostGuard(budget_yuan=budget)
        logger.info("CostGuard 已创建：budget=%.2f 元", budget)
    return _cost_guard


def _record_and_check(node_name: str, usage: Usage) -> None:
    """把单次调用用量记入全局 CostGuard 并执行预算检查。

    Args:
        node_name: 发起调用的节点名。
        usage: 本次调用的 token 用量统计。

    Raises:
        BudgetExceededError: 累计成本超出预算时抛出。
    """
    _, _, _, model = _resolve_config()
    guard = get_cost_guard()
    guard.record(
        node_name,
        {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        },
        model=model,
    )
    guard.check()


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _resolve_config() -> tuple[str, ProviderConfig, str, str]:
    """根据环境变量解析提供商配置。

    Returns:
        四元组 ``(provider_name, config, api_key, model)``。

    Raises:
        LLMConfigError: 提供商名称非法或对应 API Key 未配置。
    """
    name = os.environ.get(ENV_LLM_PROVIDER, DEFAULT_PROVIDER).strip().lower()
    config = PROVIDER_CONFIGS.get(name)
    if config is None:
        supported = ", ".join(PROVIDER_CONFIGS)
        raise LLMConfigError(f"不支持的提供商：{name!r}，可选值：{supported}")

    api_key = os.environ.get(config.api_key_env, "")
    if not api_key:
        raise LLMConfigError(
            f"环境变量 {config.api_key_env} 未配置，无法使用 {name}"
        )

    model = os.environ.get(ENV_LLM_MODEL) or config.default_model
    return name, config, api_key, model


def _post_chat_completions(
    payload: dict[str, Any],
    config: ProviderConfig,
    api_key: str,
    provider: str,
    timeout: float,
) -> dict[str, Any]:
    """向 ``/chat/completions`` 发起一次 POST 请求。

    Args:
        payload: 请求体（OpenAI 兼容格式）。
        config: 提供商配置。
        api_key: 提供商的 API Key。
        provider: 提供商名称（仅用于日志与报错）。
        timeout: 请求超时时间（秒）。

    Returns:
        响应 JSON 反序列化后的字典。

    Raises:
        LLMRequestError: 网络请求失败、HTTP 错误或响应非法 JSON。
    """
    url = f"{config.base_url}/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMRequestError(
            f"HTTP {exc.code}：provider={provider}，响应体={detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMRequestError(
            f"网络请求失败：provider={provider}，原因={exc}"
        ) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMRequestError(
            f"响应不是合法 JSON：provider={provider}，body={body[:500]}"
        ) from exc


def _chat_completion(
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
    timeout: float,
) -> tuple[str, Usage]:
    """带指数退避重试的对话补全调用。

    失败后按 1s、2s、4s… 的间隔重试，共尝试 ``MAX_RETRIES`` 次。

    Args:
        messages: OpenAI 格式的消息列表。
        temperature: 采样温度。
        max_tokens: 输出 token 上限，None 表示使用服务端默认值。
        json_mode: 是否要求服务端强制输出 JSON（response_format）。
        timeout: 单次请求超时时间（秒）。

    Returns:
        ``(text, usage)`` 元组。

    Raises:
        LLMConfigError: 提供商配置缺失。
        LLMRequestError: 全部尝试失败后抛出最后一次的异常。
    """
    provider, config, api_key, model = _resolve_config()
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    logger.debug(
        "LLM 请求：provider=%s model=%s messages=%d json_mode=%s",
        provider,
        model,
        len(messages),
        json_mode,
    )

    last_error: LLMRequestError | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = _post_chat_completions(payload, config, api_key, provider, timeout)
            return _parse_response(data, provider)
        except LLMRequestError as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "LLM 调用失败（第 %d/%d 次），%.1fs 后重试：%s",
                    attempt,
                    MAX_RETRIES,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.error("LLM 调用连续 %d 次失败：%s", MAX_RETRIES, exc)
    assert last_error is not None
    raise last_error


def _parse_response(data: dict[str, Any], provider: str) -> tuple[str, Usage]:
    """将 OpenAI 兼容响应解析为 ``(text, usage)`` 元组。

    Args:
        data: 响应 JSON 反序列化后的字典。
        provider: 提供商名称（仅用于报错）。

    Returns:
        ``(text, usage)`` 元组。

    Raises:
        LLMRequestError: 响应缺少必需字段时抛出。
    """
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError(
            f"响应格式异常：provider={provider}，data={str(data)[:500]}"
        ) from exc

    raw_usage = data.get("usage") or {}
    usage = Usage(
        prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
        completion_tokens=int(raw_usage.get("completion_tokens", 0)),
        total_tokens=int(raw_usage.get("total_tokens", 0)),
    )
    return content, usage


def _build_messages(prompt: str, system_prompt: str | None) -> list[dict[str, str]]:
    """组装 OpenAI 格式的消息列表。

    Args:
        prompt: 用户输入的问题或指令。
        system_prompt: 可选的系统提示词。

    Returns:
        OpenAI 格式的消息列表。
    """
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取并解析 JSON 对象。

    兼容两种常见情况：纯 JSON 文本、被 Markdown 代码块包裹的 JSON。

    Args:
        text: 模型返回的原始文本。

    Returns:
        解析后的字典。

    Raises:
        LLMRequestError: 文本无法解析为 JSON 对象时抛出。
    """
    cleaned = _JSON_FENCE_PATTERN.sub("", text.strip()).strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMRequestError(
            f"模型输出不是合法 JSON：{text[:300]!r}"
        ) from exc
    if not isinstance(result, dict):
        raise LLMRequestError(f"模型输出的 JSON 不是对象：{cleaned[:300]!r}")
    return result


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def chat(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    node_name: str = "unknown",
) -> tuple[str, Usage]:
    """顶层对话入口，返回 ``(text, usage)`` 元组。

    根据环境变量自动选择提供商，内置指数退避重试。每次调用完成后
    自动把用量记入全局 CostGuard 并执行预算检查。

    Args:
        prompt: 用户输入的问题或指令。
        system_prompt: 可选的系统提示词。
        temperature: 采样温度，0~2，越大越随机。
        max_tokens: 输出 token 上限，None 表示使用服务端默认值。
        timeout: 单次请求超时时间（秒）。
        node_name: 发起调用的节点名，用于成本按节点分组统计。

    Returns:
        ``(text, usage)`` 元组：模型回答文本与 token 用量统计。

    Raises:
        LLMConfigError: 提供商配置缺失。
        LLMRequestError: 重试后仍然调用失败。
        BudgetExceededError: 累计成本超出预算。
    """
    messages = _build_messages(prompt, system_prompt)
    text, usage = _chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=False,
        timeout=timeout,
    )
    _record_and_check(node_name, usage)
    logger.info("chat 完成：node=%s tokens=%d", node_name, usage.total_tokens)
    return text, usage


def chat_json(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    node_name: str = "unknown",
) -> tuple[dict[str, Any], Usage]:
    """结构化输出入口：强制 JSON 输出并解析为字典。

    通过 ``response_format={"type": "json_object"}`` 要求服务端输出
    JSON，并对 Markdown 代码块做兜底清洗。

    注意：OpenAI 兼容接口的 json_object 模式要求提示词中出现
    "json" 字样，否则部分提供商（如 DeepSeek）会报错，调用方的
    提示词中应显式说明返回 JSON 及其格式。

    Args:
        prompt: 用户输入的问题或指令（需说明期望的 JSON 格式）。
        system_prompt: 可选的系统提示词。
        temperature: 采样温度，结构化输出建议保持 0。
        max_tokens: 输出 token 上限，None 表示使用服务端默认值。
        timeout: 单次请求超时时间（秒）。
        node_name: 发起调用的节点名，透传给成本记录，用于按节点分组统计。

    Returns:
        ``(parsed_json, usage)`` 元组：解析后的 JSON 对象与 token 用量统计。

    Raises:
        LLMConfigError: 提供商配置缺失。
        LLMRequestError: 调用失败或输出无法解析为 JSON 对象。
        BudgetExceededError: 累计成本超出预算。
    """
    combined = f"{system_prompt or ''}\n{prompt}"
    if "json" not in combined.lower():
        prompt = f"{prompt}\n\n只返回 JSON 对象，不要输出其他内容。"

    messages = _build_messages(prompt, system_prompt)
    text, usage = _chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=True,
        timeout=timeout,
    )
    _record_and_check(node_name, usage)
    logger.info("chat_json 完成：node=%s tokens=%d", node_name, usage.total_tokens)
    return _extract_json(text), usage


def accumulate_usage(tracker: dict[str, int], usage: Usage) -> dict[str, int]:
    """把单次调用的 token 用量累加到 cost_tracker 中（原地修改）。

    与 :data:`workflows.state.KBState` 的 ``cost_tracker`` 字段格式对应，
    每次调用同时使 ``calls`` 计数 +1。

    Args:
        tracker: 形如 ``{"prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "calls": 0}`` 的累计字典。
        usage: 单次调用返回的用量统计。

    Returns:
        累加后的同一个 ``tracker`` 字典（便于链式使用）。
    """
    tracker["prompt_tokens"] = tracker.get("prompt_tokens", 0) + usage.prompt_tokens
    tracker["completion_tokens"] = (
        tracker.get("completion_tokens", 0) + usage.completion_tokens
    )
    tracker["total_tokens"] = tracker.get("total_tokens", 0) + usage.total_tokens
    tracker["calls"] = tracker.get("calls", 0) + 1
    return tracker


# ---------------------------------------------------------------------------
# 手动测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. 纯本地功能测试（不发网络请求）
    fenced = '```json\n{"intent": "general_chat"}\n```'
    logger.info("JSON 清洗测试：%r -> %s", fenced, _extract_json(fenced))

    # 2. 真实调用测试（需配置 LLM_PROVIDER 与对应 API Key）
    try:
        answer, usage = chat("用一句话解释什么是路由模式。")
        logger.info("chat 回答：%s（tokens=%d）", answer, usage.total_tokens)

        result, usage = chat_json(
            '把这句话分类并返回 JSON：{"intent": "..."}，'
            "候选意图：github_search / knowledge_query / general_chat。"
            "句子：帮我搜一下 GitHub 上的多智能体框架"
        )
        logger.info("chat_json 结果：%s（tokens=%d）", result, usage.total_tokens)
    except LLMConfigError as exc:
        logger.warning("跳过真实调用测试（配置缺失）：%s", exc)
    except LLMRequestError:
        logger.exception("真实调用测试失败")
