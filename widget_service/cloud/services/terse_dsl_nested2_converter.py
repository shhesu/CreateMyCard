# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Parse TerseDSL-Nested-2 as data and convert it to standard A2UI JSONL."""

from __future__ import annotations

import ast
import io
import json
import tokenize
from dataclasses import dataclass
from typing import Any

from services.compact_dsl_a2ui_converter import (
    CompactDslConversionError,
    convert_compact_dsl_to_a2ui,
)

MAX_INPUT_LENGTH = 1_048_576
MAX_COMPONENTS = 256
MAX_NESTING_DEPTH = 32
MAX_STRING_LENGTH = 65_536
MAX_COLLECTION_ITEMS = 256
MAX_OBJECT_FIELDS = 128
_A2UI_EXPRESSION_MARKER = "__terse_a2ui_expression__:"

_FORBIDDEN_KEYS = frozenset({"__proto__", "prototype", "constructor"})
_CONTAINERS = frozenset({"Row", "Column", "List", "Stack"})
_LEAVES = frozenset({"Text", "Image", "Divider", "Progress", "Button", "Checkbox"})
_COMPONENTS = _CONTAINERS | _LEAVES
_TEXT_DESIGNS = frozenset(
    {
        "display-l", "display-m", "display-s",
        "title-l", "title-m", "title-s",
        "subtitle-l", "subtitle-m", "subtitle-s",
        "body-l", "body-m", "body-s",
        "caption-l", "caption-m",
    }
)
_BUTTON_DESIGNS = frozenset({"capsule"})
_DIVIDER_DESIGNS = frozenset({"line", "bar"})


class TerseDslNested2ConversionError(ValueError):
    """Raised when Nested-2 cannot be safely converted to A2UI."""


@dataclass(frozen=True)
class Nested2Node:
    component_type: str
    values: tuple[Any, ...]
    children: tuple[Nested2Node, ...]
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class DataReference:
    """A read-only reference to a field declared by the trailing data object."""

    path: tuple[str, ...]


@dataclass(frozen=True)
class TextExpression:
    """A restricted string concatenation expression for A2UI interpolation."""

    parts: tuple[str | DataReference, ...]


@dataclass(frozen=True)
class SystemCall:
    """A restricted event invocation that lowers to an A2UI onClick handler."""

    call: str
    args: dict[str, Any]


def convert_terse_dsl_nested2_to_a2ui(
    source: str,
    *,
    size: str,
    protocol_profile: dict[str, Any],
    task_spec: dict[str, Any] | None = None,
) -> str:
    """Convert one Nested-2 component tree and optional data declaration to A2UI."""
    root = parse_terse_dsl_nested2(source)
    _validate_system_calls(root, task_spec)
    compact_rows: list[list[Any]] = []
    _append_compact_rows(root, "root", size, compact_rows)
    if root.data is not None:
        compact_rows.append(["/model", root.data])
    compact_rows.append(["/ui/state", "ready"])
    compact_dsl = "\n".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        for row in compact_rows
    )
    try:
        converted = convert_compact_dsl_to_a2ui(
            compact_dsl,
            size=size,
            protocol_profile=protocol_profile,
        )
        return _restore_a2ui_expressions(converted)
    except CompactDslConversionError as exc:
        raise TerseDslNested2ConversionError(str(exc)) from exc


def parse_terse_dsl_nested2(source: str) -> Nested2Node:
    """Parse a component tree followed by an optional restricted data declaration."""
    if not isinstance(source, str) or not source.strip():
        raise TerseDslNested2ConversionError("TerseDSL-Nested-2 output is empty.")
    if len(source) > MAX_INPUT_LENGTH:
        raise TerseDslNested2ConversionError("TerseDSL-Nested-2 input exceeds the size limit.")
    translated_source = _python_compatible_source(source)
    try:
        module = ast.parse(translated_source, mode="exec")
    except SyntaxError as exc:
        raise TerseDslNested2ConversionError(
            f"TerseDSL-Nested-2 syntax error at line {exc.lineno}: {exc.msg}."
        ) from exc
    if not module.body or len(module.body) > 2 or not isinstance(module.body[0], ast.Expr):
        raise TerseDslNested2ConversionError(
            "TerseDSL-Nested-2 must start with exactly one component call."
        )
    data: dict[str, Any] | None = None
    if len(module.body) == 2:
        data = _parse_data_assignment(module.body[1])
    state = {"components": 0}
    root = _parse_component(module.body[0].value, 1, state)
    if root.component_type != "Column":
        raise TerseDslNested2ConversionError("The root component must be Column.")
    if not root.values or root.values[0] != "card":
        raise TerseDslNested2ConversionError('The root must use Column("card", ...).')
    root = Nested2Node(root.component_type, root.values, root.children, data)
    _validate_data_references(root, data)
    return root


def _parse_data_assignment(statement: ast.stmt) -> dict[str, Any]:
    """Accept only the trailing ``data = {...}`` declaration."""
    if (
        not isinstance(statement, ast.Assign)
        or len(statement.targets) != 1
        or not isinstance(statement.targets[0], ast.Name)
        or statement.targets[0].id != "data"
    ):
        raise TerseDslNested2ConversionError(
            'The only statement after the root call may be `data = {...}`.'
        )
    data = _json_literal_value(statement.value, 1)
    if not isinstance(data, dict) or not data:
        raise TerseDslNested2ConversionError("data must be a non-empty object literal.")
    return data


def _python_compatible_source(source: str) -> str:
    """Translate only Nested-2 literal tokens; strings and component names stay untouched."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError as exc:
        raise TerseDslNested2ConversionError(
            f"TerseDSL-Nested-2 tokenization failed: {exc.args[0]}."
        ) from exc
    translated: list[tokenize.TokenInfo] = []
    literal_names = {"true": "True", "false": "False", "null": "None"}
    for index, token in enumerate(tokens):
        value = literal_names.get(token.string, token.string)
        if token.type == tokenize.NAME and _next_token_is_colon(tokens, index):
            value = repr(token.string)
            token_type = tokenize.STRING
        else:
            token_type = token.type
        translated.append(
            tokenize.TokenInfo(
                token_type,
                value,
                token.start,
                token.end,
                token.line,
            )
        )
    return tokenize.untokenize(translated)


def _next_token_is_colon(
    tokens: list[tokenize.TokenInfo],
    index: int,
) -> bool:
    ignored = {
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.COMMENT,
    }
    for candidate in tokens[index + 1 :]:
        if candidate.type in ignored:
            continue
        return candidate.type == tokenize.OP and candidate.string == ":"
    return False


def _parse_component(node: ast.AST, depth: int, state: dict[str, int]) -> Nested2Node:
    if depth > MAX_NESTING_DEPTH:
        raise TerseDslNested2ConversionError("Component nesting exceeds 32 levels.")
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        raise TerseDslNested2ConversionError(
            "Only direct Catalog component calls are allowed."
        )
    component_type = node.func.id
    if component_type not in _COMPONENTS:
        raise TerseDslNested2ConversionError(
            f'Unsupported component type "{component_type}".'
        )
    if node.keywords:
        raise TerseDslNested2ConversionError("Keyword arguments are not allowed.")
    state["components"] += 1
    if state["components"] > MAX_COMPONENTS:
        raise TerseDslNested2ConversionError("Component count exceeds 256.")

    values: list[Any] = []
    children: list[Nested2Node] = []
    child_started = False
    for value_index, argument in enumerate(node.args):
        if _is_component_call(argument):
            child_started = True
            children.append(_parse_component(argument, depth + 1, state))
            continue
        if child_started:
            raise TerseDslNested2ConversionError(
                "Value arguments must appear before the first child."
            )
        allow_text_expression = (
            value_index == 0 and component_type in {"Text", "Button"}
        )
        values.append(_component_value(argument, depth, allow_text_expression))
    if children and component_type not in _CONTAINERS:
        raise TerseDslNested2ConversionError(
            f"{component_type} cannot contain child components."
        )
    return Nested2Node(component_type, tuple(values), tuple(children))


def _is_component_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _COMPONENTS
    )


def _component_value(
    node: ast.AST,
    depth: int,
    allow_text_expression: bool = False,
) -> Any:
    """Parse a component value, including safe data reads and text concatenation."""
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return _data_reference(node)
    if isinstance(node, ast.Call):
        return _system_call(node, depth)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if not allow_text_expression:
            raise TerseDslNested2ConversionError(
                "String concatenation is only allowed for Text.content and Button.label."
            )
        parts = _text_expression_parts(node, depth)
        return TextExpression(tuple(parts))
    return _literal_value(node, depth)


def _system_call(node: ast.Call, depth: int) -> SystemCall:
    """Parse only systemCall(call, args) as an event value inside Button options."""
    if not isinstance(node.func, ast.Name) or node.func.id != "systemCall":
        raise TerseDslNested2ConversionError(
            "Only systemCall(call, args) is allowed as a non-component call."
        )
    if node.keywords or len(node.args) != 2:
        raise TerseDslNested2ConversionError(
            "systemCall requires exactly a call string and an args object."
        )
    call = _json_literal_value(node.args[0], depth + 1)
    args = _json_literal_value(node.args[1], depth + 1)
    if not isinstance(call, str) or not call or not isinstance(args, dict):
        raise TerseDslNested2ConversionError(
            "systemCall requires a non-empty call string and an args object."
        )
    return SystemCall(call, args)


def _literal_value(node: ast.AST, depth: int) -> Any:
    if depth > MAX_NESTING_DEPTH:
        raise TerseDslNested2ConversionError("Literal nesting exceeds 32 levels.")
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and len(node.value) > MAX_STRING_LENGTH:
            raise TerseDslNested2ConversionError("String literal exceeds the size limit.")
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return node.value
    if isinstance(node, ast.List):
        if len(node.elts) > MAX_COLLECTION_ITEMS:
            raise TerseDslNested2ConversionError("Array literal exceeds the item limit.")
        return [_component_value(item, depth + 1) for item in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _literal_value(node.operand, depth + 1)
        if isinstance(operand, bool) or not isinstance(operand, (int, float)):
            raise TerseDslNested2ConversionError(
                "Unary signs are only allowed on numeric literals."
            )
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Dict):
        if len(node.keys) > MAX_OBJECT_FIELDS:
            raise TerseDslNested2ConversionError("Object literal exceeds the field limit.")
        result: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            key = _literal_value(key_node, depth + 1)
            if not isinstance(key, str):
                raise TerseDslNested2ConversionError("Object keys must be strings.")
            if key in _FORBIDDEN_KEYS:
                raise TerseDslNested2ConversionError(f'Forbidden object key "{key}".')
            if key in result:
                raise TerseDslNested2ConversionError(f'Duplicate object key "{key}".')
            result[key] = _component_value(value_node, depth + 1)
        return result
    raise TerseDslNested2ConversionError(
        "Only string, number, boolean, null, array, and object literals are allowed."
    )


def _json_literal_value(node: ast.AST, depth: int) -> Any:
    """Parse the declared data object without allowing references or expressions."""
    if depth > MAX_NESTING_DEPTH:
        raise TerseDslNested2ConversionError("Literal nesting exceeds 32 levels.")
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and len(node.value) > MAX_STRING_LENGTH:
            raise TerseDslNested2ConversionError("String literal exceeds the size limit.")
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return node.value
    if isinstance(node, ast.List):
        if len(node.elts) > MAX_COLLECTION_ITEMS:
            raise TerseDslNested2ConversionError("Array literal exceeds the item limit.")
        return [_json_literal_value(item, depth + 1) for item in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _json_literal_value(node.operand, depth + 1)
        if isinstance(operand, bool) or not isinstance(operand, (int, float)):
            raise TerseDslNested2ConversionError(
                "Unary signs are only allowed on numeric literals."
            )
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Dict):
        if len(node.keys) > MAX_OBJECT_FIELDS:
            raise TerseDslNested2ConversionError("Object literal exceeds the field limit.")
        result: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            key = _json_literal_value(key_node, depth + 1)
            if not isinstance(key, str):
                raise TerseDslNested2ConversionError("Object keys must be strings.")
            if key in _FORBIDDEN_KEYS:
                raise TerseDslNested2ConversionError(f'Forbidden object key "{key}".')
            if key in result:
                raise TerseDslNested2ConversionError(f'Duplicate object key "{key}".')
            result[key] = _json_literal_value(value_node, depth + 1)
        return result
    raise TerseDslNested2ConversionError(
        "data may contain only JSON literal values."
    )


def _data_reference(node: ast.Attribute | ast.Subscript) -> DataReference:
    """Resolve ``data.identifier[0]...`` access chains to a data reference."""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        if isinstance(current, ast.Attribute):
            if current.attr.startswith("_") or current.attr in _FORBIDDEN_KEYS:
                raise TerseDslNested2ConversionError("Unsafe data field reference.")
            parts.append(current.attr)
            current = current.value
            continue
        index = current.slice
        if (
            not isinstance(index, ast.Constant)
            or isinstance(index.value, bool)
            or not isinstance(index.value, int)
            or index.value < 0
        ):
            raise TerseDslNested2ConversionError(
                "Data array indexes must be non-negative integer literals."
            )
        parts.append(str(index.value))
        current = current.value
    if not isinstance(current, ast.Name) or current.id != "data":
        expression = ast.unparse(node)
        raise TerseDslNested2ConversionError(
            "Data references must use the form data.field.subField or data.list[0].field; "
            f'received "{expression}".'
        )
    return DataReference(tuple(reversed(parts)))


def _text_expression_parts(node: ast.AST, depth: int) -> list[str | DataReference]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text_expression_parts(node.left, depth + 1) + _text_expression_parts(
            node.right, depth + 1
        )
    value = _component_value(node, depth)
    if isinstance(value, (str, DataReference)):
        return [value]
    raise TerseDslNested2ConversionError(
        "Text concatenation supports only string literals and data references."
    )


def _validate_data_references(node: Nested2Node, data: dict[str, Any] | None) -> None:
    for value in node.values:
        _validate_value_references(value, data)
    for child in node.children:
        _validate_data_references(child, data)


def _validate_value_references(value: Any, data: dict[str, Any] | None) -> None:
    if isinstance(value, DataReference):
        if data is None or not _data_path_exists(data, value.path):
            dotted = ".".join(value.path)
            raise TerseDslNested2ConversionError(
                f"Data reference data.{dotted} is not declared by data."
            )
        return
    if isinstance(value, TextExpression):
        for part in value.parts:
            _validate_value_references(part, data)
        return
    if isinstance(value, SystemCall):
        return
    if isinstance(value, dict):
        for child in value.values():
            _validate_value_references(child, data)
        return
    if isinstance(value, list):
        for child in value:
            _validate_value_references(child, data)


def _data_path_exists(data: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = data
    for part in path:
        if isinstance(current, dict):
            if part not in current:
                return False
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return False
    return True


def _validate_system_calls(
    node: Nested2Node,
    task_spec: dict[str, Any] | None,
) -> None:
    allowed = _candidate_system_calls(task_spec)
    for value in node.values:
        _validate_system_call_value(
            value,
            allowed,
            component_type=node.component_type,
        )
    for child in node.children:
        _validate_system_calls(child, task_spec)


def _candidate_system_calls(
    task_spec: dict[str, Any] | None,
) -> set[str]:
    if not isinstance(task_spec, dict):
        return set()
    candidates = task_spec.get("eventCandidates")
    if not isinstance(candidates, list):
        return set()
    return {
        _stable_json({"call": item.get("call"), "args": item.get("args")})
        for item in candidates
        if isinstance(item, dict)
        and isinstance(item.get("call"), str)
        and item.get("call")
        and isinstance(item.get("args"), dict)
    }


def _validate_system_call_value(
    value: Any,
    allowed: set[str],
    *,
    component_type: str,
    inside_on_click: bool = False,
) -> None:
    if isinstance(value, SystemCall):
        if component_type != "Button" or not inside_on_click:
            raise TerseDslNested2ConversionError(
                "systemCall is only allowed in Button onClick options."
            )
        handler = {"call": value.call, "args": value.args}
        if _stable_json(handler) not in allowed:
            raise TerseDslNested2ConversionError(
                "systemCall is not present in TaskSpec.eventCandidates."
            )
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_system_call_value(
                child,
                allowed,
                component_type=component_type,
                inside_on_click=key in {"onClick", "onclick"},
            )
        return
    if isinstance(value, list):
        for child in value:
            _validate_system_call_value(
                child,
                allowed,
                component_type=component_type,
                inside_on_click=inside_on_click,
            )


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_compact_rows(
    node: Nested2Node,
    component_id: str,
    size: str,
    rows: list[list[Any]],
) -> None:
    child_ids = [
        f"{component_id}_{index}" for index in range(len(node.children))
    ]
    props = _component_props(node, component_id, size)
    row: list[Any] = [component_id, node.component_type, props]
    if node.component_type in _CONTAINERS:
        row.append(child_ids)
    rows.append(row)
    for child, child_id in zip(node.children, child_ids, strict=True):
        _append_compact_rows(child, child_id, size, rows)


def _component_props(
    node: Nested2Node,
    component_id: str,
    size: str,
) -> dict[str, Any]:
    if node.component_type in _CONTAINERS:
        return _container_props(node, component_id, size)
    if node.component_type == "Text":
        return _designed_leaf_props(node, "content", _TEXT_DESIGNS)
    if node.component_type == "Image":
        return _leaf_props(node, "src")
    if node.component_type == "Button":
        return _button_props(node)
    if node.component_type == "Progress":
        return _progress_props(node)
    if node.component_type == "Checkbox":
        props = {}
        _merge_options(props, node.values)
        return props
    if node.component_type == "Divider":
        return _divider_props(node)
    raise TerseDslNested2ConversionError(
        f"{component_id}: unsupported component conversion."
    )


def _designed_leaf_props(
    node: Nested2Node,
    required_name: str,
    designs: frozenset[str],
) -> dict[str, Any]:
    if not node.values:
        raise TerseDslNested2ConversionError(
            f"{node.component_type} requires {required_name}."
        )
    props = {required_name: _lower_component_value(node.values[0])}
    remaining = list(node.values[1:])
    if remaining and isinstance(remaining[0], str):
        design = remaining.pop(0)
        if design not in designs:
            raise TerseDslNested2ConversionError(
                f'Unsupported {node.component_type} design "{design}".'
            )
        # Delegate expansion to the shared Compact converter. Its design
        # catalog is the source of truth shared with design-compact-dsl.
        props["design"] = design
    _merge_options(props, remaining)
    return props


def _leaf_props(node: Nested2Node, required_name: str) -> dict[str, Any]:
    if not node.values:
        raise TerseDslNested2ConversionError(
            f"{node.component_type} requires {required_name}."
        )
    props = {required_name: _lower_component_value(node.values[0])}
    _merge_options(props, node.values[1:])
    return props


def _button_props(node: Nested2Node) -> dict[str, Any]:
    props = _designed_leaf_props(node, "label", _BUTTON_DESIGNS)
    design = node.values[1] if len(node.values) > 1 else None
    if not isinstance(design, str):
        raise TerseDslNested2ConversionError("Button requires a design.")
    return props


def _divider_props(node: Nested2Node) -> dict[str, Any]:
    values = list(node.values)
    design = values.pop(0) if values and isinstance(values[0], str) else "line"
    if design not in _DIVIDER_DESIGNS:
        raise TerseDslNested2ConversionError(f'Unsupported Divider design "{design}".')
    props = {"design": design}
    _merge_options(props, values)
    return props


def _progress_props(node: Nested2Node) -> dict[str, Any]:
    """Lower Progress numeric data reads to Compact DSL path bindings."""
    values = list(node.values)
    if len(values) != 1 or not isinstance(values[0], dict) or not values[0]:
        raise TerseDslNested2ConversionError(
            "Progress requires one non-empty props object."
        )
    props: dict[str, Any] = {}
    for key, value in values[0].items():
        if key in {"value", "total", "threshold"} and isinstance(value, DataReference):
            props[key] = {"path": "/model/" + "/".join(value.path)}
        else:
            props[key] = _lower_component_value(value)
    return props


def _merge_options(props: dict[str, Any], values: Any) -> None:
    values = list(values)
    if not values:
        return
    if len(values) != 1 or not isinstance(values[0], dict) or not values[0]:
        raise TerseDslNested2ConversionError(
            "Options must be one non-empty object in the final value position."
        )
    props.update(_lower_component_value(values[0]))


def _lower_component_value(value: Any) -> Any:
    """Lower safe Terse data reads to standard A2UI template expressions."""
    if isinstance(value, DataReference):
        return _data_template_expression(value)
    if isinstance(value, TextExpression):
        return _A2UI_EXPRESSION_MARKER + " + ".join(
            _a2ui_expression_part(part) for part in value.parts
        )
    if isinstance(value, SystemCall):
        return {"call": value.call, "args": _lower_component_value(value.args)}
    if isinstance(value, dict):
        return {
            "onClick" if key == "onclick" else key: _lower_component_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_lower_component_value(child) for child in value]
    return value


def _data_template_expression(reference: DataReference) -> str:
    return _A2UI_EXPRESSION_MARKER + _a2ui_template_reference(reference)


def _a2ui_expression_part(part: str | DataReference) -> str:
    if isinstance(part, DataReference):
        return _a2ui_template_reference(part)
    return _single_quoted_a2ui_string(part)


def _a2ui_template_reference(reference: DataReference) -> str:
    """Convert ``data.a.b`` to a standard A2UI JSON Pointer expression."""
    return "${/model/" + "/".join(reference.path) + "}"

def _single_quoted_a2ui_string(value: str) -> str:
    return (
        "'"
        + value.replace("\\", "\\\\").replace("'", "\\'")
        .replace("\n", "\\n").replace("\r", "\\r")
        + "'"
    )


def _restore_a2ui_expressions(genui: str) -> str:
    """Restore Terse-only placeholders after Compact DSL has completed validation."""
    messages = [json.loads(line) for line in genui.splitlines()]
    restored = [_restore_a2ui_expression_value(message) for message in messages]
    return "\n".join(
        json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        for message in restored
    )


def _restore_a2ui_expression_value(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_A2UI_EXPRESSION_MARKER):
        expression = value.removeprefix(_A2UI_EXPRESSION_MARKER)
        return "{{ " + expression + " }}"
    if isinstance(value, list):
        return [_restore_a2ui_expression_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _restore_a2ui_expression_value(child)
            for key, child in value.items()
        }
    return value


def _container_props(
    node: Nested2Node,
    component_id: str,
    size: str,
) -> dict[str, Any]:
    values = list(node.values)
    layout = values.pop(0) if values and isinstance(values[0], str) else None
    props: dict[str, Any] = {}
    _merge_options(props, values)
    layouts = {
        ("Column", "section"): {"width": "matchParent", "itemMargin": 8},
        ("Column", "compact"): {"width": "matchParent", "itemMargin": 4},
        ("Row", "between"): {
            "width": "matchParent",
            "itemMargin": 4,
            "justifyContent": "spaceBetween",
            "alignItems": "center",
        },
        ("Row", "actions"): {
            "width": "matchParent",
            "itemMargin": 4,
            "justifyContent": "end",
            "alignItems": "center",
        },
        ("Row", "compact"): {
            "width": "matchParent",
            "itemMargin": 4,
            "alignItems": "center",
        },
        ("List", "list"): {"width": "matchParent", "space": 4},
        ("List", "dense"): {"width": "matchParent", "space": 2},
        ("Stack", "overlay"): {"width": "matchParent", "height": "matchParent"},
    }
    if component_id == "root":
        dimensions = {
            "2x2": {"width": 160, "height": 160},
            "2x4": {"width": 320, "height": 160},
            "4x2": {"width": 320, "height": 160},
        }.get(size)
        if node.component_type != "Column" or layout != "card" or dimensions is None:
            raise TerseDslNested2ConversionError(
                'Root must be Column("card", ...) with a supported size.'
            )
        explicit_dimensions = {
            key: props.pop(key)
            for key in ("width", "height")
            if key in props
        }
        if any(
            explicit_dimensions[key] != dimensions[key]
            for key in explicit_dimensions
        ):
            raise TerseDslNested2ConversionError(
                "Root width and height must match the size-locked dimensions."
            )
        return {
            **dimensions,
            "padding": 12,
            "borderRadius": 20,
            "clip": True,
            "linearGradient": {
                "direction": "RightBottom",
                "colors": [["#FFE8F1F5", 0], ["#FFE2ECE4", 1]],
            },
            "itemMargin": 8,
            **props,
        }
    if layout is None:
        return props
    preset = layouts.get((node.component_type, layout))
    if preset is None:
        raise TerseDslNested2ConversionError(
            f'Unsupported {node.component_type} layout "{layout}".'
        )
    return {**preset, **props}
