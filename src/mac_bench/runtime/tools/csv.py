from __future__ import annotations

import csv
import io
import itertools
import json
import re
from datetime import datetime
from typing import Any, Mapping, Sequence

from .runtime import ToolContext, ToolErrorCode, ToolExecutionError, ToolResult, ToolSpec

DEFAULT_ROW_LIMIT = 20
MAX_ROW_LIMIT = 500
MAX_COLUMNS = 100
MAX_CELLS = 10_000
MAX_OUTPUT_CHARS = 100_000
MAX_CELL_CHARS = 10_000
MAX_CSV_BYTES = 50 * 1024 * 1024
DELIMITERS = {"auto", ",", "\t", ";", "|"}
ENCODINGS = {"utf-8", "utf-8-sig"}


def read_csv(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    path = str(arguments.get("path") or "").strip()
    start_row = _bounded_int(arguments.get("start_row", 1), "start_row", 1, 10_000_000)
    limit = _bounded_int(arguments.get("limit", DEFAULT_ROW_LIMIT), "limit", 1, MAX_ROW_LIMIT)
    delimiter_arg = str(arguments.get("delimiter") or "auto")
    encoding = str(arguments.get("encoding") or "utf-8-sig").lower()
    if delimiter_arg not in DELIMITERS:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"delimiter must be one of {sorted(DELIMITERS)!r}",
        )
    if encoding not in ENCODINGS:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"encoding must be one of {sorted(ENCODINGS)!r}",
        )
    safe, raw = context.environment.read_bytes(path, max_bytes=MAX_CSV_BYTES)
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            ToolErrorCode.DECODE_ERROR,
            f"CSV {path!r} cannot be decoded as {encoding}",
            details={"byte_offset": exc.start},
        ) from exc
    delimiter = _detect_delimiter(text, delimiter_arg)
    try:
        rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        first = next(rows, None)
        if first is None:
            raise ToolExecutionError(ToolErrorCode.DECODE_ERROR, f"CSV {path!r} is empty")
        has_header = _has_header(text, delimiter)
        if has_header:
            header = list(first)
        else:
            header = [f"column_{index}" for index in range(len(first))]
            rows = itertools.chain((first,), rows)
        selected_indexes = _select_columns(header, arguments.get("columns"))
        if len(selected_indexes) > MAX_COLUMNS:
            raise ToolExecutionError(
                ToolErrorCode.OUTPUT_LIMIT,
                f"selected column count exceeds {MAX_COLUMNS}",
            )
        if limit * max(1, len(selected_indexes)) > MAX_CELLS:
            raise ToolExecutionError(
                ToolErrorCode.OUTPUT_LIMIT,
                f"requested preview exceeds the {MAX_CELLS}-cell limit",
            )
        for _ in range(start_row - 1):
            if next(rows, None) is None:
                break
        selected_rows: list[list[str]] = []
        warnings: list[str] = []
        truncated = False
        output_chars = 0
        row_number = start_row
        has_more = False
        while len(selected_rows) < limit:
            row = next(rows, None)
            if row is None:
                break
            if len(row) != len(header):
                warnings.append(
                    f"Row {row_number} has {len(row)} fields; header has {len(header)}."
                )
            values: list[str] = []
            for index in selected_indexes:
                value = row[index] if index < len(row) else ""
                if len(value) > MAX_CELL_CHARS:
                    warnings.append(
                        f"Cell at row {row_number}, column {index} was truncated "
                        f"from {len(value)} to {MAX_CELL_CHARS} characters."
                    )
                    value = value[:MAX_CELL_CHARS]
                    truncated = True
                values.append(value)
            row_chars = len(json.dumps(values, ensure_ascii=False))
            if output_chars + row_chars > MAX_OUTPUT_CHARS:
                if not selected_rows:
                    raise ToolExecutionError(
                        ToolErrorCode.OUTPUT_LIMIT,
                        "one CSV record exceeds the total output budget; select fewer columns",
                        details={"row": row_number, "max_output_chars": MAX_OUTPUT_CHARS},
                    )
                truncated = True
                has_more = True
                break
            selected_rows.append(values)
            output_chars += row_chars
            row_number += 1
        if not has_more:
            has_more = next(rows, None) is not None
    except csv.Error as exc:
        raise ToolExecutionError(
            ToolErrorCode.DECODE_ERROR,
            f"invalid CSV structure in {path!r}: {exc}",
        ) from exc
    columns = []
    for output_index, source_index in enumerate(selected_indexes):
        values = [row[output_index] for row in selected_rows]
        columns.append(
            {
                "index": source_index,
                "name": header[source_index],
                "inferred_type": _infer_type(values),
            }
        )
    end_row = start_row + len(selected_rows) - 1
    version = context.environment.version(raw)
    context.observe_version(safe.relative_path, version)
    data = {
            "path": safe.input_path,
            "resolved_path": safe.resolved_path,
            "delimiter": delimiter,
            "encoding": encoding,
            "has_header": has_header,
            "columns": columns,
            "start_row": start_row,
            "end_row": end_row,
            "rows": selected_rows,
            "has_more": has_more,
            "next_row": end_row + 1 if has_more else None,
            "truncated": truncated,
            "warnings": warnings,
            "version": version,
        }
    return ToolResult.completed(
        data,
        model_text=_csv_model_text(data, explicit_columns=arguments.get("columns") is not None),
    )


def _csv_model_text(data: Mapping[str, Any], *, explicit_columns: bool) -> str:
    columns = list(data.get("columns") or ())
    names = [str(item.get("name") or item.get("index")) for item in columns]
    header = (
        f"[CSV {data.get('path')}: {len(columns)} selected column(s), "
        f"rows {data.get('start_row')}-{data.get('end_row')}]"
    )
    schema = "columns: " + ", ".join(
        f"{item.get('name')}<{item.get('inferred_type')}>" for item in columns
    )
    if len(columns) > 20 and not explicit_columns:
        return (
            f"{header}\n{schema}\n"
            "[wide table: rows omitted from model context; call read_csv again with "
            f"columns=[...] chosen from the names above and limit<=20, start_row={data.get('start_row')} ]"
        )
    lines = [header, schema, "rows (JSON arrays in column order):"]
    lines.extend(json.dumps(row, ensure_ascii=False) for row in data.get("rows") or ())
    if data.get("has_more"):
        selector = f", columns={json.dumps(names, ensure_ascii=False)}" if explicit_columns else ""
        lines.append(
            f"[more rows available; continue with start_row={data.get('next_row')}{selector}]"
        )
    for warning in data.get("warnings") or ():
        lines.append(f"[warning: {warning}]")
    return "\n".join(lines)


def _detect_delimiter(text: str, requested: str) -> str:
    if requested != "auto":
        return requested
    sample = text[:65_536]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        candidates: list[tuple[int, str]] = []
        for delimiter in (",", "\t", ";", "|"):
            try:
                first = next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
            except (csv.Error, StopIteration):
                continue
            candidates.append((len(first), delimiter))
        if candidates:
            field_count, delimiter = max(candidates)
            if field_count > 1:
                return delimiter
        raise ToolExecutionError(
            ToolErrorCode.DECODE_ERROR,
            "could not determine CSV delimiter; pass delimiter explicitly",
        )


def _has_header(text: str, delimiter: str) -> bool:
    try:
        dialect = csv.Sniffer().sniff(text[:65_536], delimiters=delimiter)
        return bool(csv.Sniffer().has_header(text[:65_536])) and dialect.delimiter == delimiter
    except csv.Error:
        return True


def _select_columns(header: Sequence[str], raw: Any) -> list[int]:
    if raw is None:
        return list(range(len(header)))
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            "columns must be an array of names or indexes",
        )
    indexes: list[int] = []
    for selector in raw:
        if isinstance(selector, bool):
            raise ToolExecutionError(
                ToolErrorCode.INVALID_ARGUMENTS,
                "boolean values are not valid column selectors",
            )
        if isinstance(selector, int):
            index = selector
        elif isinstance(selector, str):
            matches = [index for index, name in enumerate(header) if name == selector]
            if not matches:
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"unknown column {selector!r}",
                )
            if len(matches) > 1:
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"column name {selector!r} is ambiguous; select by index",
                )
            index = matches[0]
        else:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"invalid column selector {selector!r}",
            )
        if index < 0 or index >= len(header):
            raise ToolExecutionError(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"column index {index} is out of range",
            )
        indexes.append(index)
    return indexes


def _infer_type(values: Sequence[str]) -> str:
    nonempty = [value for value in values if value != ""]
    if not nonempty:
        return "string"
    predicates = (
        ("boolean", lambda value: value.lower() in {"true", "false", "yes", "no"}),
        ("integer", lambda value: bool(re.fullmatch(r"[+-]?\d+", value))),
        ("float", lambda value: _is_float(value)),
        ("date", lambda value: _is_date(value)),
    )
    matches = [name for name, predicate in predicates if all(predicate(value) for value in nonempty)]
    if matches:
        return matches[0]
    primitive_kinds = {
        next((name for name, predicate in predicates if predicate(value)), "string")
        for value in nonempty
    }
    return "string" if primitive_kinds == {"string"} else "mixed"


def _is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _is_date(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"{name} must be between {minimum} and {maximum}",
        )
    return parsed


def read_csv_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="read_csv",
        description="Preview CSV records and selected columns without performing analysis.",
        properties={
            "path": {"type": "string"},
            "start_row": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROW_LIMIT},
            "columns": {
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "delimiter": {"type": "string", "enum": ["auto", ",", "\t", ";", "|"]},
            "encoding": {"type": "string", "enum": ["utf-8", "utf-8-sig"]},
        },
        required=("path",),
        handler=read_csv,
    )
