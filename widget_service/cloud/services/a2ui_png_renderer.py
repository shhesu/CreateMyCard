"""PNG preview renderer for the supported OpenHarmony A2UI extension subset."""

import json
import math
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


class A2uiPngRenderer:
    """Render standard three-line A2UI JSONL into a local PNG preview."""

    def render(self, genui: str, output_path: Path) -> None:
        messages = [json.loads(line) for line in genui.splitlines() if line.strip()]
        surface = messages[0]["createSurface"]
        update = messages[1]["updateComponents"]
        self.nodes = {node["id"]: node for node in update["components"]}
        self.data = messages[2]["updateDataModel"]["value"]
        image = Image.new("RGB", (surface["width"], surface["height"]), "white")
        self.image = image
        self.canvas = ImageDraw.Draw(image, "RGBA")
        self._render(update["root"], (0, 0, image.width, image.height))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path, "PNG")

    def _render(self, node_id: str, box: tuple[int, int, int, int]) -> None:
        node = self.nodes[node_id]
        styles = node.get("styles", {})
        x, y, width, height = box
        self._paint_background((x, y, width, height), styles)
        padding = styles.get("padding", 0)
        if isinstance(padding, dict):
            left, top = padding.get("left", 0), padding.get("top", 0)
            right, bottom = padding.get("right", 0), padding.get("bottom", 0)
        else:
            left = top = right = bottom = padding
        content_box = (
            x + left,
            y + top,
            max(1, width - left - right),
            max(1, height - top - bottom),
        )
        kind = node["component"]
        if kind == "Text":
            self._text(str(self._resolve(node.get("content", ""))), content_box, styles)
            return
        if kind == "Button":
            self._paint_background(
                box, {**styles, "backgroundColor": styles.get("backgroundColor", "#190A59F7")}
            )
            self._text(
                str(self._resolve(node.get("label", ""))),
                content_box,
                {
                    **styles,
                    "textAlign": styles.get("textAlign", "center"),
                    "fontColor": styles.get("fontColor", styles.get("textColor", "#FFFFFFFF")),
                },
            )
            return
        if kind == "Progress":
            self._progress(content_box, node, styles)
            return
        if kind == "Image":
            self._image(node.get("src", ""), box, styles)
            return
        children = node.get("children", [])
        gap = node.get("itemMargin", 0)
        if kind == "Row":
            self._render_linear(children, content_box, gap, horizontal=True, styles=styles)
        elif kind == "Stack":
            for child in children:
                self._render(child, content_box)
        else:
            self._render_linear(children, content_box, gap, horizontal=False, styles=styles)

    def _render_linear(
        self,
        children: list[str],
        box: tuple[int, int, int, int],
        gap: int,
        *,
        horizontal: bool,
        styles: dict[str, Any],
    ) -> None:
        """Lay out a Row/Column with fixed, intrinsic and weighted children."""
        if not children:
            return
        x, y, width, height = box
        main_axis = width if horizontal else height
        # Gaps are decorative. Reduce them before shrinking meaningful content.
        gap = min(gap, max(0, main_axis // max(1, len(children) * 3)))
        available = main_axis - gap * (len(children) - 1)
        fixed_sizes: dict[str, int] = {}
        weighted: list[str] = []
        total_weight = 0.0
        for child_id in children:
            child = self.nodes[child_id]
            child_styles = child.get("styles", {})
            dimension = child_styles.get("width" if horizontal else "height")
            if isinstance(dimension, (int, float)):
                fixed_sizes[child_id] = max(1, int(dimension))
            elif child_styles.get("layoutWeight") is not None:
                weight = max(1.0, float(child_styles["layoutWeight"]))
                weighted.append(child_id)
                total_weight += weight
            else:
                fixed_sizes[child_id] = self._intrinsic_main_size(child, horizontal, width, height)
        # A weighted content area (typically the card's main metric) must retain
        # some room even when fixed header/footer content is taller than the surface.
        weighted_reserve = min(available // 2, 24 * len(weighted))
        fixed_budget = max(0, available - weighted_reserve)
        fixed_total = sum(fixed_sizes.values())
        if fixed_total > fixed_budget:
            explicit = {
                child_id: size
                for child_id, size in fixed_sizes.items()
                if isinstance(
                    self.nodes[child_id].get("styles", {}).get("width" if horizontal else "height"),
                    (int, float),
                )
            }
            flexible = set(fixed_sizes) - set(explicit)
            flexible_space = max(0, fixed_budget - sum(explicit.values()))
            flexible_total = sum(fixed_sizes[child_id] for child_id in flexible)
            if flexible_total:
                for child_id in flexible:
                    fixed_sizes[child_id] = max(
                        1, int(fixed_sizes[child_id] * flexible_space / flexible_total)
                    )
            fixed_total = sum(fixed_sizes.values())
        remaining = max(weighted_reserve, available - fixed_total) if weighted else 0
        sizes = dict(fixed_sizes)
        for child_id in weighted:
            weight = max(1.0, float(self.nodes[child_id].get("styles", {}).get("layoutWeight", 1)))
            sizes[child_id] = max(1, int(remaining * weight / total_weight))
        used = sum(sizes.values()) + gap * (len(children) - 1)
        extra = max(0, (width if horizontal else height) - used)
        justify = styles.get("justifyContent", "start")
        cursor = x if horizontal else y
        between = gap
        if justify == "center":
            cursor += extra // 2
        elif justify in {"end", "bottom", "right"}:
            cursor += extra
        elif justify == "spaceBetween" and len(children) > 1:
            between += extra // (len(children) - 1)
        for child_id in children:
            main_size = sizes[child_id]
            cross_size = height if horizontal else width
            cross_position = y if horizontal else x
            child_styles = self.nodes[child_id].get("styles", {})
            alignment = child_styles.get("alignSelf") or styles.get("alignItems", "start")
            intrinsic_cross = self._intrinsic_cross_size(
                self.nodes[child_id], horizontal, width, height
            )
            if alignment in {"center", "middle"}:
                cross_position += max(0, (cross_size - intrinsic_cross) // 2)
                cross_size = min(cross_size, intrinsic_cross)
            elif alignment in {"end", "bottom", "right"}:
                cross_position += max(0, cross_size - intrinsic_cross)
                cross_size = min(cross_size, intrinsic_cross)
            if horizontal:
                self._render(child_id, (cursor, cross_position, main_size, cross_size))
            else:
                self._render(child_id, (cross_position, cursor, cross_size, main_size))
            cursor += main_size + between

    def _intrinsic_main_size(
        self, node: dict[str, Any], horizontal: bool, width: int, height: int
    ) -> int:
        styles = node.get("styles", {})
        if node["component"] in {"Text", "Button"}:
            font = self._font(int(styles.get("fontSize", 14)), styles.get("fontWeight", 400))
            text = str(self._resolve(node.get("content", node.get("label", ""))))
            if horizontal:
                return min(width, max(1, int(self.canvas.textlength(text, font=font)) + 2))
            return max(int(styles.get("fontSize", 14)) + 6, 20)
        if node["component"] == "Image":
            return int(styles.get("width" if horizontal else "height", 20))
        children = node.get("children", [])
        if children:
            gap = int(node.get("itemMargin", 0))
            child_horizontal = node["component"] == "Row"
            dimensions = [
                self._intrinsic_main_size(self.nodes[child], horizontal, width, height)
                for child in children
            ]
            if child_horizontal == horizontal:
                return sum(dimensions) + gap * max(0, len(children) - 1)
            return max(dimensions, default=0)
        return 0

    def _intrinsic_cross_size(
        self, node: dict[str, Any], horizontal: bool, width: int, height: int
    ) -> int:
        styles = node.get("styles", {})
        dimension = styles.get("height" if horizontal else "width")
        if isinstance(dimension, (int, float)):
            return max(1, int(dimension))
        return self._intrinsic_main_size(node, not horizontal, width, height)

    def _paint_background(self, box: tuple[int, int, int, int], styles: dict[str, Any]) -> None:
        color = styles.get("backgroundColor")
        gradient = styles.get("linearGradient")
        if isinstance(gradient, dict) and isinstance(gradient.get("colors"), list):
            stops = self._gradient_stops(gradient["colors"])
            x, y, width, height = box
            angle = math.radians(float(gradient.get("angle", 180)))
            direction_x, direction_y = math.sin(angle), -math.cos(angle)
            corners = (
                0.0,
                direction_x * width,
                direction_y * height,
                direction_x * width + direction_y * height,
            )
            projection_start, projection_end = min(corners), max(corners)
            projection_span = max(1.0, projection_end - projection_start)
            for offset_y in range(height):
                for offset_x in range(width):
                    projection = direction_x * offset_x + direction_y * offset_y
                    ratio = (projection - projection_start) / projection_span
                    self.canvas.point(
                        (x + offset_x, y + offset_y), fill=self._gradient_color(stops, ratio)
                    )
        elif color:
            x, y, width, height = box
            self.canvas.rounded_rectangle(
                (x, y, x + width, y + height),
                radius=styles.get("borderRadius", 0),
                fill=self._color(color),
            )

    def _text(self, value: str, box: tuple[int, int, int, int], styles: dict[str, Any]) -> None:
        x, y, width, height = box
        size = int(styles.get("fontSize", 14))
        font = self._font(size, styles.get("fontWeight", 400))
        value = self._truncate(value, font, width, styles.get("textOverflow") == "ellipsis")
        text_box = self.canvas.textbbox((0, 0), value, font=font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        if text_height > height:
            return
        text_align = styles.get("textAlign", "start")
        if text_align in {"center", "centre"}:
            text_x = x + max(0, (width - text_width) // 2)
        elif text_align in {"end", "right"}:
            text_x = x + max(0, width - text_width)
        else:
            text_x = x
        # Text must not paint over neighbouring components when a compact card
        # assigns a smaller line box than the requested font size.
        text_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        text_canvas = ImageDraw.Draw(text_layer)
        text_canvas.text(
            (
                text_x - x,
                max(0, (height - text_height) // 2) - text_box[1],
            ),
            value,
            font=font,
            fill=self._color(styles.get("fontColor", "#E5000000")),
        )
        self.image.paste(text_layer, (x, y), text_layer)

    def _font(self, size: int, weight: Any = 400) -> ImageFont.FreeTypeFont:
        font_name = "PingFang.ttc" if int(weight) < 600 else "PingFang.ttc"
        try:
            return ImageFont.truetype(f"/System/Library/Fonts/{font_name}", size, index=0)
        except OSError:
            return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", size)

    def _image(self, source: str, box: tuple[int, int, int, int], styles: dict[str, Any]) -> None:
        x, y, width, height = box
        asset_path = Path(__file__).resolve().parents[3] / source
        try:
            if asset_path.suffix.lower() == ".svg":
                raise OSError("SVG rasterisation is unavailable in the local preview runtime")
            asset = Image.open(asset_path).convert("RGBA")
            asset.thumbnail((width, height))
            self.image.paste(
                asset,
                (x + (width - asset.width) // 2, y + (height - asset.height) // 2),
                asset,
            )
        except (OSError, ValueError):
            # The card contract commonly supplies SVGs. Keep the preview deterministic
            # even on hosts without a native SVG rasteriser.
            self.canvas.rounded_rectangle(
                (x, y, x + width, y + height),
                radius=styles.get("borderRadius", 4),
                fill=self._color("#14000000"),
            )
            self._text(
                self._image_symbol(source),
                (x, y, width, height),
                {"fontSize": min(width, height)},
            )

    @staticmethod
    def _image_symbol(source: str) -> str:
        source = source.lower()
        if "clock" in source:
            return "◷"
        if "moon" in source:
            return "☾"
        if "bell" in source:
            return "♩"
        if "weather" in source or "sun" in source:
            return "☀"
        return "●"

    def _truncate(
        self, value: str, font: ImageFont.FreeTypeFont, width: int, ellipsis: bool
    ) -> str:
        if self.canvas.textlength(value, font=font) <= width:
            return value
        suffix = "…" if ellipsis else ""
        while value and self.canvas.textlength(value + suffix, font=font) > width:
            value = value[:-1]
        return value + suffix

    def _progress(
        self, box: tuple[int, int, int, int], node: dict[str, Any], styles: dict[str, Any]
    ) -> None:
        x, y, width, height = box
        value = float(self._resolve(node.get("value", 0)) or 0)
        total = float(self._resolve(node.get("total", 100)) or 100)
        self.canvas.rounded_rectangle(
            (x, y, x + width, y + height), radius=height // 2, fill=self._color("#22000000")
        )
        self.canvas.rounded_rectangle(
            (x, y, x + width * min(1, value / max(1, total)), y + height),
            radius=height // 2,
            fill=self._color(styles.get("color", "#FF0A59F7")),
        )

    def _resolve(self, value: Any) -> Any:
        if isinstance(value, dict):
            value = value.get("path", "")
        if not isinstance(value, str):
            return value
        path_match = re.search(r"\$\{(/[^}]+)\}", value)
        if path_match is None:
            return value
        path = path_match.group(1)
        current: Any = self.data
        for token in path.strip("/").split("/"):
            current = current.get(token, "") if isinstance(current, dict) else ""
        if value.strip().startswith("{{"):
            return current
        return (
            re.sub(r"\$\{(/[^}]+)\}", str(current), value).replace("'' + ", "").replace(" + ''", "")
        )

    @staticmethod
    def _color(value: str) -> tuple[int, int, int, int] | str:
        if not isinstance(value, str) or not value.startswith("#") or len(value) != 9:
            return value
        alpha, red, green, blue = (int(value[index : index + 2], 16) for index in range(1, 9, 2))
        return red, green, blue, alpha

    def _gradient_stops(self, colors: list[Any]) -> list[tuple[tuple[int, int, int, int], float]]:
        stops: list[tuple[tuple[int, int, int, int], float]] = []
        for index, item in enumerate(colors):
            if not isinstance(item, list) or not item:
                continue
            color = self._color(item[0])
            if not isinstance(color, tuple):
                continue
            offset = float(item[1]) if len(item) > 1 else index / max(1, len(colors) - 1)
            stops.append((color, min(1.0, max(0.0, offset))))
        return stops or [((255, 255, 255, 255), 0.0)]

    @staticmethod
    def _gradient_color(
        stops: list[tuple[tuple[int, int, int, int], float]], ratio: float
    ) -> tuple[int, int, int, int]:
        ratio = min(1.0, max(0.0, ratio))
        for right_index, (_, right_offset) in enumerate(stops):
            if ratio <= right_offset:
                if right_index == 0:
                    return stops[0][0]
                left_color, left_offset = stops[right_index - 1]
                right_color, _ = stops[right_index]
                local_ratio = (ratio - left_offset) / max(0.0001, right_offset - left_offset)
                return tuple(
                    round(left * (1 - local_ratio) + right * local_ratio)
                    for left, right in zip(left_color, right_color, strict=True)
                )
        return stops[-1][0]

    def _mix(self, start: str, end: str, ratio: float) -> tuple[int, int, int, int]:
        first, second = self._color(start), self._color(end)
        if not isinstance(first, tuple) or not isinstance(second, tuple):
            return 255, 255, 255, 255
        return tuple(round(a * (1 - ratio) + b * ratio) for a, b in zip(first, second, strict=True))
