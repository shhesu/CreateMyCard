#!/usr/bin/env python3
"""Run every request file in a directory against a WebSocket card-generation API."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets


@dataclass
class CaseResult:
    response: dict[str, Any] | None
    error: str | None
    artifact_url: str | None = None
    artifact_markdown: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send every case file verbatim to a WebSocket and save its result."
    )
    parser.add_argument("--ws-url", required=True, help="WebSocket endpoint, e.g. ws://host/path")
    parser.add_argument(
        "--cases-dir", type=Path, required=True, help="Directory containing case files"
    )
    parser.add_argument(
        "--result-dir",
        type=Path,
        help="Output directory (default: <cases-dir>/result)",
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-case timeout in seconds")
    return parser.parse_args()


async def run_case(ws_url: str, payload: str, timeout: float) -> CaseResult:
    """Send one text frame and wait for the final JSON response frame."""
    try:
        async with websockets.connect(ws_url, open_timeout=timeout) as websocket:
            await websocket.send(payload)
            while True:
                raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                if message.get("errorCode") not in (None, "0", 0):
                    return CaseResult(message, str(message.get("errorMessage") or "Unknown error"))
                stream_type = (
                    message.get("reply", {}).get("streamInfo", {}).get("streamType")
                )
                if stream_type == "final" or stream_type is None:
                    if message.get("errorCode") in ("0", 0):
                        return CaseResult(message, None)
    except Exception as exc:  # The report must be produced even for transport failures.
        return CaseResult(None, f"{type(exc).__name__}: {exc}")


def extract_artifact_url(response: dict[str, Any]) -> str | None:
    stream_content = (
        response.get("reply", {}).get("streamInfo", {}).get("streamContent", "")
    )
    if not isinstance(stream_content, str):
        return None
    markdown_link = re.search(r"artifactUrl[^\n]*?\]\((https?://[^)]+)\)", stream_content)
    if markdown_link:
        return markdown_link.group(1)
    url = re.search(r"https?://[^\s'\"<>]+", stream_content)
    return url.group(0) if url else None


def download_markdown(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "widget-ws-case-runner/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read().decode("utf-8")


def extract_code_blocks(markdown: str, language: str) -> list[str]:
    pattern = rf"```{re.escape(language)}\s*\n(.*?)```"
    return re.findall(pattern, markdown, flags=re.IGNORECASE | re.DOTALL)


def output_base(case_file: Path, cases_dir: Path, result_dir: Path) -> Path:
    relative = case_file.relative_to(cases_dir).with_suffix("")
    return result_dir / relative


def write_result(base: Path, case_file: Path, result: CaseResult) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    report = [f"# {case_file.name}", ""]
    if result.error:
        report.extend(["## Error", "", result.error, ""])
    else:
        report.extend(["## Status", "", "success", ""])
    if result.artifact_url:
        report.extend(["## Artifact URL", "", result.artifact_url, ""])
    if result.artifact_markdown is not None:
        artifact_file = base.with_suffix(".artifact.md")
        artifact_file.write_text(result.artifact_markdown, encoding="utf-8")
        for language, fence_names in (
            ("genui", ("genui",)),
            ("designcompactdsl", ("designcompactdsl", "design-compact-dsl")),
        ):
            blocks = [
                block
                for fence_name in fence_names
                for block in extract_code_blocks(result.artifact_markdown, fence_name)
            ]
            if blocks:
                code_file = base.with_suffix(f".{language}")
                code_file.write_text("\n\n".join(blocks).strip() + "\n", encoding="utf-8")
                report.extend(
                    [f"## {language}", "", f"```{language}", blocks[0].strip(), "```", ""]
                )
    if result.response is not None:
        response_json = json.dumps(result.response, ensure_ascii=False, indent=2)
        report.extend(["## Response", "", "```json", response_json, "```", ""])
    base.with_suffix(".result.md").write_text("\n".join(report), encoding="utf-8")


async def main() -> int:
    args = parse_args()
    cases_dir = args.cases_dir.resolve()
    result_dir = (args.result_dir or cases_dir / "result").resolve()
    if not cases_dir.is_dir():
        print(f"cases directory does not exist: {cases_dir}", file=sys.stderr)
        return 2
    case_files = sorted(
        path
        for path in cases_dir.rglob("*")
        if path.is_file() and result_dir not in path.parents
    )
    if not case_files:
        print(f"no case files found in: {cases_dir}", file=sys.stderr)
        return 2

    failures = 0
    for case_file in case_files:
        result = await run_case(args.ws_url, case_file.read_text(encoding="utf-8"), args.timeout)
        if result.error is None and result.response is not None:
            result.artifact_url = extract_artifact_url(result.response)
            if result.artifact_url:
                try:
                    result.artifact_markdown = download_markdown(result.artifact_url, args.timeout)
                except Exception as exc:
                    result.error = f"artifact download failed: {type(exc).__name__}: {exc}"
        if result.error:
            failures += 1
        write_result(output_base(case_file, cases_dir, result_dir), case_file, result)
        status = "FAIL" if result.error else "PASS"
        print(f"[{status}] {case_file.relative_to(cases_dir)}")
    print(f"completed {len(case_files)} case(s), failures: {failures}; results: {result_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
