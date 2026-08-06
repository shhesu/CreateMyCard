# -*- coding: utf-8 -*-
"""Build and validate the first-round card template selection."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models.generation import TaskSpec
from services.json_loader import load_json

_PROFILE_DIR = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "protocol_profiles"
    / "terse-dsl-nested-2"
    / "template-selection"
)


@dataclass(frozen=True)
class CardTemplateSelection:
    template_id: str
    business_type: str
    reason: str
    confidence: float
    template_source: str

    def prompt_context(self) -> dict[str, Any]:
        return {
            "templateId": self.template_id,
            "businessType": self.business_type,
            "reason": self.reason,
            "confidence": self.confidence,
            "templateTerseDslNested2": self.template_source,
            "instruction": (
                "模板只提供组件层级、信息分区与布局角色参考。必须独立生成并允许修改 root"
                "背景、渐变、颜色、字体、间距、圆角及全部组件属性；替换业务文案、数据绑定、"
                "素材和动作。不要照抄模板中的像素尺寸、样式对象或占位资源；最终语法、Design "
                "Token、LayoutPreset、根尺寸和能力约束始终以原系统Prompt与TaskSpec为准。"
            ),
        }


class CardTemplateSelector:
    def __init__(self, profile_dir: Path | None = None) -> None:
        self.profile_dir = profile_dir or _PROFILE_DIR
        catalog = load_json(self.profile_dir / "catalog.json")
        if not isinstance(catalog, dict) or not isinstance(catalog.get("templates"), list):
            raise ValueError("Card template catalog must contain templates")
        self.catalog = catalog
        self.templates = {
            item["templateId"]: item
            for item in catalog["templates"]
            if isinstance(item, dict) and isinstance(item.get("templateId"), str)
        }

    def build_prompt(self, task_spec: TaskSpec) -> list[dict[str, str]]:
        system_prompt = (self.profile_dir / "PROMPT.md").read_text(encoding="utf-8")
        user_payload = {
            "taskSpec": task_spec.model_dump(mode="json", exclude_none=True),
            "templateCatalog": self.catalog,
        }
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]

    def parse(self, raw_output: str) -> CardTemplateSelection:
        payload = self._parse_json_object(raw_output)
        template_id = payload.get("templateId")
        if not isinstance(template_id, str) or template_id not in self.templates:
            raise ValueError("Template selector returned an unknown templateId")
        business_type = payload.get("businessType")
        declared_types = self.templates[template_id].get("businessTypes", [])
        if not isinstance(business_type, str) or business_type not in declared_types:
            raise ValueError("Template selector returned an undeclared businessType")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 80:
            raise ValueError("Template selector returned an invalid reason")
        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ValueError("Template selector returned an invalid confidence")
        confidence_value = float(confidence)
        if not 0 <= confidence_value <= 1:
            raise ValueError("Template selector confidence must be between 0 and 1")
        template_path = self.profile_dir / "templates" / f"{template_id}.nested-2"
        return CardTemplateSelection(
            template_id=template_id,
            business_type=business_type,
            reason=reason.strip(),
            confidence=confidence_value,
            template_source=template_path.read_text(encoding="utf-8").strip(),
        )

    @staticmethod
    def _parse_json_object(raw_output: str) -> dict[str, Any]:
        text = raw_output.strip()
        if text.startswith("```json"):
            text = text[len("```json") :].strip()
            if text.endswith("```"):
                text = text[:-3].strip()
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Template selector output must be a JSON object")
        return payload
