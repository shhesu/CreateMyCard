# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""OpenAI 兼容的流式 LLM 客户端。"""

import json
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Literal

import requests

from app.logger import logger

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
_MODULE = "[DeepSeek LLM Client]"


@dataclass
class LLMClientOptions:
    """OpenAI 兼容模型调用的配置。"""

    api_key: str
    model: str = DEEPSEEK_DEFAULT_MODEL
    api_url: str = DEEPSEEK_URL
    temperature: float = 0.4
    thinking_mode: Literal["enabled", "disabled"] = "disabled"
    timeout_seconds: float = 180.0


def _parse_sse_event(data: str) -> tuple[str, dict | None]:
    """解析一个 OpenAI 兼容 SSE 事件。"""
    if data == "[DONE]":
        return "", None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return "", None

    usage = payload.get("usage")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", usage if isinstance(usage, dict) else None
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", usage if isinstance(usage, dict) else None
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return "", usage if isinstance(usage, dict) else None
    content = delta.get("content")
    parsed_usage = usage if isinstance(usage, dict) else None
    return content if isinstance(content, str) else "", parsed_usage


async def stream_genui(
    options: LLMClientOptions,
    messages: list[dict],
    *,
    _on_usage: Callable[[dict], None] | None = None,
) -> AsyncGenerator[str, None]:
    """调用 OpenAI 兼容 SSE 接口并逐片段产出正文。"""
    if not options.api_key.strip():
        raise ValueError("Missing DeepSeek API key")
    headers = {
        "Authorization": f"Bearer {options.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "model": options.model,
        "messages": messages,
        "stream": True,
        "temperature": options.temperature,
        "thinking": {"type": options.thinking_mode},
        "stream_options": {"include_usage": True},
    }
    started_at = time.perf_counter()
    logger.info(
        f"{_MODULE} request_dispatching model={options.model} "
        f"thinking_mode={options.thinking_mode} "
        f"api_url={options.api_url} message_count={len(messages)} "
        f"message_chars={sum(len(str(item.get('content', ''))) for item in messages)}"
    )
    try:
        with requests.post(
            options.api_url,
            headers=headers,
            json=body,
            stream=True,
            timeout=options.timeout_seconds,
        ) as response:
            response.raise_for_status()
            headers_latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.info(
                f"{_MODULE} response_headers_received status_code={response.status_code} "
                f"latency_ms={headers_latency_ms} "
                f"request_id={response.headers.get('x-request-id', '')}"
            )
            received_first_event = False
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                if not received_first_event:
                    first_event_latency_ms = round(
                        (time.perf_counter() - started_at) * 1000,
                        2,
                    )
                    logger.info(
                        f"{_MODULE} first_sse_event_received "
                        f"latency_ms={first_event_latency_ms}"
                    )
                    received_first_event = True
                content, usage = _parse_sse_event(raw_line.removeprefix("data:").strip())
                if usage is not None and _on_usage is not None:
                    _on_usage(usage)
                if content:
                    yield content
    except requests.RequestException as exc:
        logger.error(
            f"{_MODULE} request_failed exception_type={type(exc).__name__} "
            f"exception={exc!r}"
        )
        raise RuntimeError(f"DeepSeek request failed: {exc}") from exc
