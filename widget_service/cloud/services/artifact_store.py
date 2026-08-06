# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import json
import os
from pathlib import Path

from app.logger import logger
from config.config import get_settings
from models.artifact import WidgetArtifact
from models.service import ArtifactSaveResult
from services.a2ui_png_renderer import A2uiPngRenderer
from services.source_artifact_repository import calculate_artifact_digest
from utils.file import save_txt_file

_MODULE = "[Artifact Store]"


class ArtifactStore:
    def save(self, artifact: WidgetArtifact) -> ArtifactSaveResult:
        """保存 artifact 并返回访问地址和摘要。

        入参：
        - artifact：完整卡片产物。
        出参：artifact 保存结果，包含访问 URL 和 sha256 摘要。
        """
        artifact_data = artifact.model_dump(mode="json", exclude_none=True)
        payload_bytes = len(
            json.dumps(
                artifact_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest = calculate_artifact_digest(artifact)
        logger.info(
            f"{_MODULE} artifact_payload_built payload_bytes={payload_bytes} digest={digest}"
        )

        # Artifact 以具名 Markdown 代码块保存。每个块名与对应契约字段一致，
        # 既保留端侧现有的 genui/cardspec 解析方式，也完整携带排障和回放信息。
        json_blocks = {
            "cardspec": artifact_data["cardSpec"],
            "schema": {"schemaVersion": artifact_data["schemaVersion"]},
            "taskspec": artifact_data["taskSpec"],
            "prompt": artifact_data["modelPrompt"],
            "effectivecapabilities": artifact_data["effectiveCapabilities"],
            "removedcapabilities": artifact_data["removedCapabilities"],
            "generationplan": artifact_data["generationPlan"],
            "meta": artifact_data["meta"],
        }
        blocks = [
            "```cardspec\n"
            + json.dumps(json_blocks["cardspec"], ensure_ascii=False, indent=2)
            + "\n```",
            f"```genui\n{artifact.genui}\n```",
        ]
        if artifact.designCompactDsl:
            blocks.append(f"```designcompactdsl\n{artifact.designCompactDsl}\n```")
        blocks.extend(
            "```" + name + "\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"
            for name, value in json_blocks.items()
            if name != "cardspec"
        )
        file_content = "\n".join(blocks) + "\n"

        # UUID 同时进入 meta 和对象名，避免毫秒时间戳在并发生成时发生覆盖。
        file_name = f"artifact_{artifact.meta.artifactId}.md"
        settings = get_settings()
        file_path = os.path.join(str(settings.WORKSPACE_ROOT), file_name)
        save_txt_file(file_path, file_content)
        logger.info(f"{_MODULE} artifact_file_saved path={file_path}")
        artifact_url = f"{settings.artifact_base_url.rstrip('/')}/{file_name}"
        preview_name = f"preview_{artifact.meta.artifactId}.png"
        preview_path = Path(settings.WORKSPACE_ROOT) / "previews" / preview_name
        preview_base_url = settings.artifact_base_url.rsplit("/artifacts", 1)[0]
        preview_url = ""
        try:
            A2uiPngRenderer().render(artifact.genui, preview_path)
            preview_url = f"{preview_base_url}/previews/{preview_name}"
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            # A preview is auxiliary: malformed/unsupported A2UI must not prevent
            # the card artifact itself from being returned to the caller.
            logger.warning(
                f"{_MODULE} preview_render_failed "
                f"artifact_id={artifact.meta.artifactId} error={error}"
            )
        logger.info(f"{_MODULE} artifact_saved_locally artifact_url={artifact_url}")
        return ArtifactSaveResult(
            artifactUrl=artifact_url,
            artifactDigest=digest,
            previewUrl=preview_url,
        )
