# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import asyncio
import math
import time

from app.logger import json_for_log, logger
from custom.llmclient import LLMClientOptions, stream_genui

_MODULE = "[LLMClient Model Transport]"


class LlmClientModelTransport:
    """适配现有 llmclient 流，保持其实现不变。"""

    def generate(self, messages: list[dict[str, str]]) -> str:
        """聚合 llmclient 的流式 Token，返回未经 DSL 处理的完整文本。"""
        usage: dict = {}

        def collect_usage(value: dict) -> None:
            usage.update(value)

        async def collect_stream() -> str:
            options = LLMClientOptions(api_key="AccessService")
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
        source = "usage"
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            prompt_tokens = _estimate_tokens("".join(item.get("content", "") for item in messages))
            completion_tokens = _estimate_tokens(result)
            source = "estimated"
        logger.info(
            f"{_MODULE} llm_call_metrics prompt_tokens={prompt_tokens} "
            f"completion_tokens={completion_tokens} api_latency_ms={duration_ms} "
            f"token_source={source}"
        )
        logger.info(f"{_MODULE} response_collected content={json_for_log(result)}")
        return result


def _estimate_tokens(value: str) -> int:
    """Conservative fallback used only when an OpenAI-compatible usage block is absent."""
    return math.ceil(len(value.encode("utf-8")) / 4) if value else 0
