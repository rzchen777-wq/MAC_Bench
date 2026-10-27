"""JSON helpers for LLM proposal payloads."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from jsonschema import Draft202012Validator


class ProposalParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ProposalParseError("empty", "model returned empty output")
    candidates = [text]
    fenced = _FENCE_RE.findall(text)
    candidates.extend(item.strip() for item in fenced)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    # Also try extracting a bare array
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, Mapping):
            return dict(payload)
        if isinstance(payload, list):
            return {"children": payload}
        last_error = ProposalParseError("parse_error", "JSON root must be an object or array")
    if arr_start != -1 and arr_end > arr_start:
        try:
            payload = json.loads(text[arr_start : arr_end + 1])
            if isinstance(payload, list):
                return {"children": payload}
        except json.JSONDecodeError as exc:
            last_error = exc
    message = f"invalid JSON: {last_error}" if last_error else "invalid JSON"
    raise ProposalParseError("parse_error", message)


def validate_schema(payload: Mapping[str, Any], schema: Mapping[str, Any], *, label: str) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.path) or "$"
        raise ProposalParseError("schema_error", f"{label} {path}: {first.message}")
