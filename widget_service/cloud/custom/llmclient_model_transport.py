# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import asyncio
import time

from app.logger import json_for_log, logger
from config.config import Settings, get_settings
from custom.llmclient import LLMClientOptions, stream_genui

_MODULE = "[LLMClient Model Transport]"


class LlmClientModelTransport:
    """适配现有 llmclient 流，保持其实现不变。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.last_metrics: dict[str, int | float | None] = {}
        self.settings = settings or get_settings()

    def generate(self, messages: list[dict[str, str]]) -> str:
        """聚合 llmclient 的流式 Token，返回未经 DSL 处理的完整文本。"""
        usage: dict = {}

        def collect_usage(value: dict) -> None:
            usage.update(value)

        async def collect_stream() -> str:
            options = LLMClientOptions(
                api_key=self.settings.deepseek_api_key,
                api_url=self.settings.deepseek_api_url,
                model=self.settings.deepseek_model,
                temperature=self.settings.model_temperature,
                thinking_mode=self.settings.deepseek_thinking_mode,
                timeout_seconds=self.settings.deepseek_timeout_seconds,
            )
            chunks = [
                chunk
                async for chunk in stream_genui(
                    options,
                    messages,
                    _on_usage=collect_usage,
                )
            ]
            return "".join(chunks)

        started_at = time.perf_counter()
        result = asyncio.run(collect_stream())
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get(
            "completion_tokens",
            usage.get("output_tokens"),
        )
        has_usage = isinstance(prompt_tokens, int) and isinstance(completion_tokens, int)
        self.last_metrics = {
            "promptTokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
            "completionTokens": completion_tokens if isinstance(completion_tokens, int) else None,
            "apiLatencyMs": duration_ms,
        }
        logger.info(
            f"{_MODULE} llm_call_metrics prompt_tokens={prompt_tokens} "
            f"completion_tokens={completion_tokens} api_latency_ms={duration_ms} "
            f"token_source={'usage' if has_usage else 'unavailable'}"
        )
        logger.info(f"{_MODULE} response_collected content={json_for_log(result)}")
        return result
