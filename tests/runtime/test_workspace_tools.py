from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Mapping

import pytest
from PIL import Image

from mac_bench.runtime.llm.cache import cache_key
from mac_bench.runtime.llm.client import ChatMessage
from mac_bench.runtime.llm.config import LLMConfig
from mac_bench.runtime.types import SolverWorkspace
from mac_bench.runtime.sandbox.base import CommandResult, SandboxBackend, SandboxHandle
from mac_bench.runtime.sandbox.memory import MemorySandboxBackend
from mac_bench.runtime.tools import (
    ExecutionEnvironment,
    ProcessManager,
    ToolCall,
    ToolContext,
    ToolErrorCode,
    phase3_tool_registry,
    phase4_tool_registry,
    phase5_tool_registry,
    workspace_tool_schemas,
)


class FilesystemSandboxBackend(SandboxBackend):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.fail_replace_suffix: str | None = None
        self.read_calls = 0
        self.mutate_on_read: tuple[int, Path, bytes] | None = None

    def create(self, *, name, snapshot=None, image=None, network_blocked=False):
        del snapshot, image, network_blocked
        return SandboxHandle(name, "filesystem-test", self.root)

    def snapshot_exists(self, *, name: str) -> bool:
        del name
        return False

    def create_snapshot(self, sandbox, *, name: str, timeout: int = 600) -> None:
        del sandbox, name, timeout

    def clone(self, sandbox, *, url: str, path: str, revision=None) -> None:
        del sandbox, url, path, revision

    def exec(
        self,
        sandbox,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        del sandbox
        workdir = self.root / (cwd or ".")
        completed = subprocess.run(
            command,
            cwd=workdir,
            env=dict(env) if env else None,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return CommandResult(command, completed.returncode, completed.stdout, 0.0)

    def write_file(self, sandbox, *, path: str, content: bytes) -> None:
        del sandbox
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def replace_file(self, sandbox, *, path: str, content: bytes) -> None:
        del sandbox
        target = Path(path)
        if self.fail_replace_suffix and str(target).endswith(self.fail_replace_suffix):
            raise RuntimeError("injected replace failure")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".mac-bench-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def delete_file(self, sandbox, *, path: str) -> None:
        del sandbox
        Path(path).unlink()

    def read_file(self, sandbox, *, path: str) -> bytes:
        del sandbox
        self.read_calls += 1
        if self.mutate_on_read and self.read_calls == self.mutate_on_read[0]:
            target, content = self.mutate_on_read[1:]
            target.write_bytes(content)
        return Path(path).read_bytes()

    def resolve_path(self, sandbox, *, path: str, cwd=None, kind="any") -> str:
        del sandbox
        candidate = Path(path) if Path(path).is_absolute() else self.root / (cwd or ".") / path
        resolved = candidate.resolve(strict=kind != "write")
        if kind == "write":
            return str(resolved)
        if kind == "file" and not resolved.is_file():
            raise IsADirectoryError(path)
        if kind == "directory" and not resolved.is_dir():
            raise NotADirectoryError(path)
        return str(resolved)

    def set_network_blocked(self, sandbox, *, blocked: bool) -> None:
        del sandbox, blocked

    def delete(self, sandbox) -> None:
        del sandbox


@pytest.fixture
def tool_context(tmp_path: Path) -> tuple[ToolContext, Path]:
    workspace_root = tmp_path / "workspace" / "repo"
    workspace_root.mkdir(parents=True)
    backend = FilesystemSandboxBackend(tmp_path)
    workspace = SolverWorkspace(
        sandbox=SandboxHandle("test", "filesystem-test", tmp_path),
        cwd="workspace/repo",
    )
    environment = ExecutionEnvironment(backend, workspace)
    context = ToolContext(
        environment=environment,
        agent_id="agent-1",
        sample_id="sample-1",
        permissions=frozenset({"read", "search", "read_csv", "read_image", "apply_patch"}),
        capabilities=frozenset({"image_input"}),
    )
    return context, workspace_root


def test_contract_schemas_and_capability_gating(tool_context) -> None:
    context, _ = tool_context
    names = [item["function"]["name"] for item in workspace_tool_schemas(image_input=True)]
    assert names == ["exec", "process", "read", "search", "apply_patch", "read_image", "read_csv"]
    assert "read_image" not in {
        item["function"]["name"] for item in workspace_tool_schemas(image_input=False)
    }

    registry = phase3_tool_registry()
    assert registry.execute("unknown_tool", {"path": "x.png"}, context).error[
        "code"
    ] == ToolErrorCode.INVALID_ARGUMENTS
    no_vision = ToolContext(
        environment=context.environment,
        agent_id="agent-1",
        sample_id="sample-1",
        permissions=context.permissions,
    )
    assert "read_image" not in {
        item["function"]["name"] for item in registry.schemas(no_vision)
    }

    phase5 = phase5_tool_registry()
    full_context = ToolContext(
        environment=context.environment,
        agent_id=context.agent_id,
        sample_id=context.sample_id,
        permissions=frozenset({*context.permissions, "exec", "process"}),
        capabilities=context.capabilities,
    )
    advertised = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in workspace_tool_schemas(image_input=True)
    }
    executable = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in phase5.schemas(full_context)
    }
    for name in ("exec", "process", "apply_patch"):
        assert advertised[name] == executable[name]

    wrong_type = registry.execute("read", {"path": "x", "limit": "many"}, context)
    unknown_field = registry.execute("read", {"path": "x", "surprise": True}, context)
    assert wrong_type.error["code"] == ToolErrorCode.INVALID_ARGUMENTS
    assert unknown_field.error["code"] == ToolErrorCode.INVALID_ARGUMENTS

    call = ToolCall.from_openai(
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read", "arguments": '{"path":"missing.txt"}'},
        }
    )
    assert call.call_id == "call-1"
    assert registry.execute_call(call, context).error["code"] == ToolErrorCode.FILE_NOT_FOUND


def test_read_is_paginated_versioned_and_rejects_workspace_escape(tool_context) -> None:
    context, root = tool_context
    (root / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    outside = root.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "linked.txt").symlink_to(outside)
    registry = phase3_tool_registry()

    result = registry.execute("read", {"path": "notes.txt", "offset": 2, "limit": 1}, context)
    assert result.status == "completed"
    assert result.data["content"] == "2| two"
    assert result.data["next_offset"] == 3
    assert str(result.data["version"]).startswith("sha256:")
    assert result.openai_content() == (
        "2| two\n\n[read notes.txt: lines 2-2 of 3; continue with offset=3]"
    )

    traversal = registry.execute("read", {"path": "../secret.txt"}, context)
    absolute_inside = registry.execute("read", {"path": str(root / "notes.txt")}, context)
    absolute_outside = registry.execute("read", {"path": str(outside)}, context)
    symlink = registry.execute("read", {"path": "linked.txt"}, context)
    assert absolute_inside.status == "completed"
    assert absolute_inside.data["resolved_path"] == str(root / "notes.txt")
    assert absolute_outside.error["code"] == ToolErrorCode.PATH_OUTSIDE_WORKSPACE
    assert traversal.error["code"] == ToolErrorCode.PATH_OUTSIDE_WORKSPACE
    assert symlink.error["code"] == ToolErrorCode.PATH_OUTSIDE_WORKSPACE


def test_read_rejects_binary_and_suggests_read_csv(tool_context) -> None:
    context, root = tool_context
    (root / "binary.bin").write_bytes(b"a\x00b")
    (root / "table.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    registry = phase3_tool_registry()

    binary = registry.execute("read", {"path": "binary.bin"}, context)
    table = registry.execute("read", {"path": "table.csv"}, context)
    assert binary.error["code"] == ToolErrorCode.UNSUPPORTED_FORMAT
    assert "read_csv" in table.data["warnings"][0]


def test_read_consumes_an_overlong_line_without_repeating_the_cursor(tool_context) -> None:
    context, root = tool_context
    (root / "long.txt").write_text("x" * 100_001 + "\nnext\n", encoding="utf-8")
    result = phase3_tool_registry().execute("read", {"path": "long.txt"}, context)
    assert result.status == "completed"
    assert result.data["end_line"] == 1
    assert result.data["next_offset"] == 2
    assert "truncated" in result.data["warnings"][0]


def test_search_uses_real_ripgrep_with_context_and_pagination(tool_context) -> None:
    context, root = tool_context
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("before\nNeedle here\nafter\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("needle again\n", encoding="utf-8")
    registry = phase3_tool_registry()

    result = registry.execute(
        "search",
        {
            "query": "needle",
            "path": "src",
            "target": "content",
            "context_lines": 1,
            "limit": 1,
        },
        context,
    )
    assert result.status == "completed"
    assert result.data["total_matches"] == 2
    assert result.data["next_offset"] == 1
    assert result.data["matches"][0]["before"] == ["before"]
    assert result.data["matches"][0]["after"] == ["after"]
    assert result.openai_content() == (
        "[search query='needle' path='src': 1 of 2 match(es)]\n"
        "src/a.py-1- before\n"
        "src/a.py:2:1: Needle here\n"
        "src/a.py-3- after\n"
        "[more matches available; continue with offset=1]"
    )

    files = registry.execute(
        "search",
        {"query": "*.py", "path": "src", "target": "files"},
        context,
    )
    assert [item["path"] for item in files.data["matches"]] == ["src/a.py", "src/b.py"]


def test_search_distinguishes_no_matches_from_invalid_regex(tool_context) -> None:
    context, root = tool_context
    (root / "a.txt").write_text("abc", encoding="utf-8")
    registry = phase3_tool_registry()
    missing = registry.execute("search", {"query": "zzz"}, context)
    invalid = registry.execute("search", {"query": "[", "regex": True}, context)
    assert missing.status == "completed"
    assert missing.data["matches"] == []
    assert missing.data["error"] is None
    assert invalid.status == "failed"
    assert invalid.error["code"] == ToolErrorCode.INVALID_ARGUMENTS

    one_file = registry.execute(
        "search", {"query": "abc", "path": "a.txt", "target": "content"}, context
    )
    assert one_file.status == "completed"


def test_search_falls_back_to_grep_when_ripgrep_is_missing(
    tool_context, monkeypatch
) -> None:
    context, root = tool_context
    (root / "a.txt").write_text("first line\nneedle here\n", encoding="utf-8")
    backend = context.environment.backend
    original_exec = backend.exec

    def without_rg(sandbox, command, **kwargs):
        if command.startswith("rg "):
            return CommandResult(command, 127, "/bin/sh: rg: command not found", 0.0)
        return original_exec(sandbox, command, **kwargs)

    monkeypatch.setattr(backend, "exec", without_rg)
    result = phase3_tool_registry().execute(
        "search", {"query": "needle", "path": "."}, context
    )

    assert result.status == "completed"
    assert result.data["matches"][0]["path"].endswith("a.txt")
    assert result.data["matches"][0]["line"] == 2
    assert "used grep fallback" in result.data["warnings"][0]
    assert "needle here" in str(result.openai_content())

    invalid = phase3_tool_registry().execute(
        "search", {"query": "[", "regex": True}, context,
    )
    assert invalid.status == "failed"
    assert invalid.error["code"] == ToolErrorCode.INVALID_ARGUMENTS
    missing = phase3_tool_registry().execute("search", {"query": "absent"}, context)
    assert missing.status == "completed" and missing.data["matches"] == []
    (root / "many.txt").write_text("needle\n" * 10000)
    limited = phase3_tool_registry().execute("search", {"query": "needle", "limit": 1}, context)
    assert limited.status == "completed" and limited.data["next_offset"] == 1


def test_read_csv_handles_bom_multiline_records_and_raw_values(tool_context) -> None:
    context, root = tool_context
    (root / "data.csv").write_text(
        '\ufeffid,description,zip\n0001,"hello,\nworld",02138\n0002,plain,10001\n',
        encoding="utf-8",
    )
    registry = phase3_tool_registry()
    result = registry.execute(
        "read_csv",
        {"path": "data.csv", "columns": ["id", "description", "zip"], "limit": 1},
        context,
    )
    assert result.status == "completed"
    assert result.data["rows"] == [["0001", "hello,\nworld", "02138"]]
    assert result.data["has_more"] is True
    assert result.data["next_row"] == 2
    assert [item["inferred_type"] for item in result.data["columns"]] == [
        "integer",
        "string",
        "integer",
    ]
    model_text = str(result.openai_content())
    assert model_text.startswith("[CSV data.csv: 3 selected column(s), rows 1-1]")
    assert '["0001", "hello,\\nworld", "02138"]' in model_text
    assert "continue with start_row=2" in model_text


def test_read_csv_wide_table_model_output_is_schema_first(tool_context) -> None:
    context, root = tool_context
    headers = [f"column_{index}" for index in range(25)]
    (root / "wide.csv").write_text(
        ",".join(headers) + "\n" + ",".join(str(index) for index in range(25)) + "\n",
        encoding="utf-8",
    )

    result = phase3_tool_registry().execute("read_csv", {"path": "wide.csv"}, context)

    assert result.status == "completed"
    model_text = str(result.openai_content())
    assert "wide table: rows omitted" in model_text
    assert "columns=[...]" in model_text
    assert '[["0"' not in model_text


def test_read_csv_duplicate_names_require_index_and_truncation_is_explicit(tool_context) -> None:
    context, root = tool_context
    (root / "duplicate.csv").write_text(
        f"score,score,description\n0.7,0.8,{'x' * 10001}\n",
        encoding="utf-8",
    )
    registry = phase3_tool_registry()
    ambiguous = registry.execute(
        "read_csv", {"path": "duplicate.csv", "columns": ["score"]}, context
    )
    selected = registry.execute(
        "read_csv", {"path": "duplicate.csv", "columns": [0, 1, 2]}, context
    )
    assert ambiguous.error["code"] == ToolErrorCode.INVALID_ARGUMENTS
    assert selected.data["rows"][0][:2] == ["0.7", "0.8"]
    assert selected.data["truncated"] is True
    assert "truncated" in selected.data["warnings"][0]


def test_read_csv_rejects_a_single_record_over_total_output_budget(tool_context) -> None:
    context, root = tool_context
    headers = ",".join(f"c{index}" for index in range(11))
    row = ",".join("x" * 10_000 for _ in range(11))
    (root / "wide.csv").write_text(f"{headers}\n{row}\n", encoding="utf-8")
    result = phase3_tool_registry().execute("read_csv", {"path": "wide.csv"}, context)
    assert result.status == "failed"
    assert result.error["code"] == ToolErrorCode.OUTPUT_LIMIT


def test_read_image_returns_native_blocks_resizes_and_redacts_trace(tool_context) -> None:
    context, root = tool_context
    path = root / "wide.png"
    Image.new("RGB", (3000, 1000), color=(20, 40, 60)).save(path)
    result = phase3_tool_registry().execute(
        "read_image", {"path": "wide.png", "detail": "high"}, context
    )
    assert result.status == "completed"
    assert result.data["original_width"] == 3000
    assert result.data["width"] == 2048
    assert result.data["resized"] is True
    openai = result.openai_content()
    assert isinstance(openai, list)
    assert openai[1]["type"] == "image_url"
    assert openai[1]["image_url"]["url"].startswith("data:image/png;base64,")
    anthropic = result.anthropic_content()
    assert anthropic[1]["type"] == "image"
    responses = result.openai_responses_content()
    assert responses[1]["type"] == "input_image"

    message = ChatMessage("tool", openai, tool_call_id="call-image")
    live = message.as_dict()
    trace = message.trace_dict()
    assert "base64," in json.dumps(live)
    assert "base64," not in json.dumps(trace)
    assert trace["content"][1]["image_url"]["data_url_sha256"]


def test_read_image_applies_exif_orientation_and_rejects_gif(tool_context) -> None:
    context, root = tool_context
    jpeg = root / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (40, 20), color="red").save(jpeg, exif=exif)
    gif = root / "animated.gif"
    Image.new("RGB", (10, 10), color="blue").save(gif, format="GIF")
    registry = phase3_tool_registry()

    rotated = registry.execute(
        "read_image", {"path": "rotated.jpg", "detail": "original"}, context
    )
    unsupported = registry.execute("read_image", {"path": "animated.gif"}, context)
    assert (rotated.data["width"], rotated.data["height"]) == (20, 40)
    assert unsupported.error["code"] == ToolErrorCode.UNSUPPORTED_FORMAT


def test_multimodal_cache_key_hashes_image_content_without_storing_data_url(tool_context) -> None:
    context, root = tool_context
    Image.new("RGB", (8, 8), color="green").save(root / "tiny.png")
    result = phase3_tool_registry().execute("read_image", {"path": "tiny.png"}, context)
    message = ChatMessage("tool", result.openai_content(), tool_call_id="call-image")
    first = cache_key(model="m", messages=[message])
    second = cache_key(model="m", messages=[message])
    changed = ChatMessage(
        "tool",
        [{"type": "text", "text": "different"}],
        tool_call_id="call-image",
    )
    assert first == second
    assert first != cache_key(model="m", messages=[changed])




def test_llm_config_explicitly_gates_image_input() -> None:
    base = {"base_url": "https://example.invalid/v1", "model": "m"}
    enabled = LLMConfig.from_mapping({**base, "image_input": True}, environ={})
    disabled = LLMConfig.from_mapping(base, environ={})
    assert enabled.image_input is True
    assert disabled.image_input is False




def test_phase3_tools_run_on_memory_sandbox_backend() -> None:
    backend = MemorySandboxBackend()
    sandbox = backend.create(name="memory-tools")
    workspace = SolverWorkspace(sandbox=sandbox, cwd="workspace/repo")
    backend.write_file(
        sandbox,
        path="workspace/repo/data.csv",
        content=b"name,value\nalpha,1\nbeta,2\n",
    )
    backend.write_file(
        sandbox,
        path="evaluator/secret.txt",
        content=b"beta must not be visible",
    )
    context = ToolContext(
        environment=ExecutionEnvironment(backend, workspace),
        agent_id="agent-1",
        sample_id="memory-smoke",
        permissions=frozenset({"read", "search", "read_csv"}),
    )
    registry = phase3_tool_registry()

    read_result = registry.execute("read", {"path": "data.csv", "limit": 2}, context)
    csv_result = registry.execute("read_csv", {"path": "data.csv", "limit": 1}, context)
    search_result = registry.execute("search", {"query": "beta"}, context)

    assert read_result.status == "completed"
    assert csv_result.data["rows"] == [["alpha", "1"]]
    assert search_result.data["total_matches"] == 1
    assert search_result.data["matches"][0]["path"] == "data.csv"


def test_apply_patch_supports_atomic_multi_file_operations(tool_context) -> None:
    context, root = tool_context
    (root / "update.txt").write_text("header\nold\ntail\n", encoding="utf-8")
    (root / "update.txt").chmod(0o755)
    (root / "delete.txt").write_text("remove me\n", encoding="utf-8")
    (root / "move.txt").write_text("before\n", encoding="utf-8")
    registry = phase4_tool_registry()

    result = registry.execute(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Update File: update.txt
@@
 header
-old
+new
 tail
*** Add File: nested/added.txt
+created
*** Delete File: delete.txt
*** Update File: move.txt
*** Move to: moved.txt
@@
-before
+after
*** End Patch"""
        },
        context,
    )

    assert result.status == "completed"
    assert (root / "update.txt").read_text() == "header\nnew\ntail\n"
    assert (root / "update.txt").stat().st_mode & 0o777 == 0o755
    assert (root / "nested/added.txt").read_text() == "created\n"
    assert not (root / "delete.txt").exists()
    assert not (root / "move.txt").exists()
    assert (root / "moved.txt").read_text() == "after\n"
    assert {item["operation"] for item in result.data["files"]} == {
        "added",
        "updated",
        "deleted",
    }
    assert "update.txt" in result.data["diff"]


def test_apply_patch_preflight_failure_modifies_nothing(tool_context) -> None:
    context, root = tool_context
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    (root / "b.txt").write_text("two\n", encoding="utf-8")
    result = phase4_tool_registry().execute(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Update File: a.txt
@@
-one
+changed
*** Update File: b.txt
@@
-missing
+changed
*** End Patch"""
        },
        context,
    )

    assert result.status == "failed"
    assert result.error["code"] == ToolErrorCode.INVALID_PATCH.value
    assert result.error["details"] == {"path": "b.txt", "hunk": 1}
    assert (root / "a.txt").read_text() == "one\n"
    assert (root / "b.txt").read_text() == "two\n"


def test_apply_patch_detects_stale_read_and_symlink_escape(tool_context, tmp_path: Path) -> None:
    context, root = tool_context
    target = root / "state.txt"
    target.write_text("seen\n", encoding="utf-8")
    registry = phase4_tool_registry()
    assert registry.execute("read", {"path": "state.txt"}, context).status == "completed"
    target.write_text("external\n", encoding="utf-8")

    stale = registry.execute(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Update File: state.txt
@@
-external
+agent
*** End Patch"""
        },
        context,
    )
    assert stale.error["code"] == ToolErrorCode.VERSION_CONFLICT.value
    assert target.read_text() == "external\n"

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    escaped = registry.execute(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Add File: escape/owned.txt
+no
*** End Patch"""
        },
        context,
    )
    assert escaped.error["code"] == ToolErrorCode.PATH_OUTSIDE_WORKSPACE.value
    assert not (outside / "owned.txt").exists()


def test_apply_patch_rolls_back_an_incomplete_commit(tool_context) -> None:
    context, root = tool_context
    (root / "a.txt").write_text("a-old\n", encoding="utf-8")
    (root / "b.txt").write_text("b-old\n", encoding="utf-8")
    backend = context.environment.backend
    assert isinstance(backend, FilesystemSandboxBackend)
    backend.fail_replace_suffix = "b.txt"

    result = phase4_tool_registry().execute(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Update File: a.txt
@@
-a-old
+a-new
*** Update File: b.txt
@@
-b-old
+b-new
*** End Patch"""
        },
        context,
    )

    assert result.status == "failed"
    assert result.error["code"] == ToolErrorCode.EXECUTION_ERROR.value
    assert (root / "a.txt").read_text() == "a-old\n"
    assert (root / "b.txt").read_text() == "b-old\n"


def test_apply_patch_rechecks_versions_after_preflight(tool_context) -> None:
    context, root = tool_context
    target = root / "race.txt"
    target.write_text("old\n", encoding="utf-8")
    backend = context.environment.backend
    assert isinstance(backend, FilesystemSandboxBackend)
    backend.mutate_on_read = (2, target, b"external\n")

    result = phase4_tool_registry().execute(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Update File: race.txt
@@
-old
+agent
*** End Patch"""
        },
        context,
    )
    assert result.error["code"] == ToolErrorCode.VERSION_CONFLICT.value
    assert target.read_text() == "external\n"


def test_apply_patch_lock_order_avoids_multi_file_deadlock(tool_context) -> None:
    context, root = tool_context
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    first = ToolContext(
        environment=context.environment,
        agent_id="first",
        sample_id=context.sample_id,
        permissions=context.permissions,
    )
    second = ToolContext(
        environment=context.environment,
        agent_id="second",
        sample_id=context.sample_id,
        permissions=context.permissions,
    )
    registry = phase4_tool_registry()
    for agent in (first, second):
        registry.execute("read", {"path": "a.txt"}, agent)
        registry.execute("read", {"path": "b.txt"}, agent)
    patches = [
        """*** Begin Patch
*** Update File: a.txt
@@
-a
+a1
*** Update File: b.txt
@@
-b
+b1
*** End Patch""",
        """*** Begin Patch
*** Update File: b.txt
@@
-b
+b2
*** Update File: a.txt
@@
-a
+a2
*** End Patch""",
    ]
    import threading

    barrier = threading.Barrier(2)
    results = []

    def run(agent, patch):
        barrier.wait()
        results.append(registry.execute("apply_patch", {"patch": patch}, agent))

    threads = [
        threading.Thread(target=run, args=(first, patches[0])),
        threading.Thread(target=run, args=(second, patches[1])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(result.status for result in results) == ["completed", "failed"]
    assert any(
        result.error and result.error["code"] == ToolErrorCode.VERSION_CONFLICT.value
        for result in results
    )


def test_apply_patch_runs_on_memory_backend() -> None:
    backend = MemorySandboxBackend()
    sandbox = backend.create(name="memory-patch")
    backend.write_file(sandbox, path="workspace/repo/old.txt", content=b"old\n")
    context = ToolContext(
        environment=ExecutionEnvironment(
            backend, SolverWorkspace(sandbox=sandbox, cwd="workspace/repo")
        ),
        agent_id="agent",
        sample_id="memory-patch",
        permissions=frozenset({"apply_patch"}),
    )
    result = phase4_tool_registry().execute(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Update File: old.txt
@@
-old
+new
*** Add File: added.txt
+added
*** End Patch"""
        },
        context,
    )
    assert result.status == "completed"
    assert backend.read_file(sandbox, path="workspace/repo/old.txt") == b"new\n"
    assert backend.read_file(sandbox, path="workspace/repo/added.txt") == b"added\n"
    backend.delete(sandbox)


def _memory_process_context(
    *, agent_id: str = "agent-1"
) -> tuple[MemorySandboxBackend, SandboxHandle, ToolContext]:
    backend = MemorySandboxBackend()
    sandbox = backend.create(name=f"process-{agent_id}")
    backend.write_file(sandbox, path="workspace/repo/.keep", content=b"")
    context = ToolContext(
        environment=ExecutionEnvironment(
            backend,
            SolverWorkspace(sandbox=sandbox, cwd="workspace/repo"),
        ),
        agent_id=agent_id,
        sample_id="process-smoke",
        permissions=frozenset({"exec", "process"}),
    )
    return backend, sandbox, context


def _wait_for_process_output(registry, context, started, expected):
    output = started.data["output"]
    deadline = time.monotonic() + 5
    while expected not in output and time.monotonic() < deadline:
        result = registry.execute(
            "process", {"action": "poll", "session_id": started.data["session_id"],
                        "wait_ms": 100}, context,
        )
        assert result.status == "completed"
        output += result.data["output"]
    assert expected in output, output


def test_exec_yields_and_poll_returns_only_new_output() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    started = registry.execute(
        "exec",
        {
            "command": "python -u -c 'print(\"first\", flush=True); input(); print(\"second\")'",
            "yield_time_ms": 100,
        },
        context,
    )
    assert started.status == "completed"
    assert started.data["status"] == "running"
    _wait_for_process_output(registry, context, started, "first")
    session_id = started.data["session_id"]
    written = registry.execute(
        "process", {"action": "write", "session_id": session_id, "data": "\n"}, context,
    )
    assert written.status == "completed"

    finished = registry.execute(
        "process",
        {"action": "poll", "session_id": session_id, "wait_ms": 2_000},
        context,
    )
    assert finished.data["status"] == "completed"
    assert "first" not in finished.data["output"]
    assert "second" in finished.data["output"]
    assert finished.data["exit_code"] == 0
    empty = registry.execute(
        "process", {"action": "poll", "session_id": session_id}, context
    )
    assert empty.data["output"] == ""
    backend.delete(sandbox)


def test_process_write_eof_and_log_pagination_are_independent() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    started = registry.execute(
        "exec",
        {
            "command": "python -u -c 'import sys; value=sys.stdin.read(); print(\"got:\"+value)'",
            "yield_time_ms": 0,
        },
        context,
    )
    session_id = started.data["session_id"]
    written = registry.execute(
        "process",
        {"action": "write", "session_id": session_id, "data": "hello", "eof": True},
        context,
    )
    assert written.status == "completed"
    polled = registry.execute(
        "process",
        {"action": "poll", "session_id": session_id, "wait_ms": 2_000},
        context,
    )
    assert polled.data["status"] == "completed"
    assert "got:hello" in polled.data["output"]

    log = registry.execute(
        "process",
        {"action": "log", "session_id": session_id, "offset": 0, "limit": 1},
        context,
    )
    assert "got:hello" in log.data["output"]
    assert registry.execute(
        "process", {"action": "poll", "session_id": session_id}, context
    ).data["output"] == ""
    backend.delete(sandbox)


def test_process_timeout_scope_isolation_and_listing() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    started = registry.execute(
        "exec",
        {"command": "sleep 5", "yield_time_ms": 0, "timeout_ms": 100},
        context,
    )
    session_id = started.data["session_id"]
    timed_out = registry.execute(
        "process",
        {"action": "poll", "session_id": session_id, "wait_ms": 1_000},
        context,
    )
    assert timed_out.data["status"] == "failed"
    assert timed_out.data["signal"] == "TIMEOUT"

    listed = registry.execute("process", {"action": "list"}, context)
    assert [item["session_id"] for item in listed.data["sessions"]] == [session_id]
    other = ToolContext(
        environment=context.environment,
        agent_id="agent-2",
        sample_id=context.sample_id,
        permissions=context.permissions,
    )
    hidden = registry.execute(
        "process", {"action": "poll", "session_id": session_id}, other
    )
    assert hidden.error["code"] == ToolErrorCode.SESSION_NOT_FOUND.value
    assert registry.execute("process", {"action": "list"}, other).data["sessions"] == []
    backend.delete(sandbox)


def test_watchdog_does_not_timeout_an_unpolled_completed_process() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    started = registry.execute(
        "exec",
        {"command": "sleep .05; printf done", "yield_time_ms": 0, "timeout_ms": 400},
        context,
    )
    session_id = started.data["session_id"]
    import time

    time.sleep(0.6)
    result = registry.execute(
        "process", {"action": "poll", "session_id": session_id}, context
    )
    assert result.data["status"] == "completed"
    assert result.data["signal"] is None
    assert "done" in result.data["output"]
    backend.delete(sandbox)


def test_process_action_validation_and_rejects_unknown_tool_name() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    started = registry.execute(
        "exec",
        {
            "command": "python -u -c 'import sys; print(sys.stdin.readline().strip())'",
            "yield_time_ms": 0,
        },
        context,
    )
    session_id = started.data["session_id"]
    written = registry.execute(
        "process",
        {"action": "write", "session_id": session_id, "data": "canonical\n"},
        context,
    )
    assert written.status == "completed"
    finished = registry.execute(
        "process",
        {"action": "poll", "session_id": session_id, "wait_ms": 2_000},
        context,
    )
    assert finished.data["status"] == "completed"
    assert "canonical" in finished.data["output"]

    unknown = registry.execute(
        "unknown_tool", {"session_id": session_id}, context
    )
    assert unknown.error["code"] == ToolErrorCode.INVALID_ARGUMENTS

    invalid = registry.execute(
        "process",
        {"action": "poll", "session_id": session_id, "offset": 2},
        context,
    )
    assert invalid.error["code"] == ToolErrorCode.INVALID_ARGUMENTS.value
    backend.delete(sandbox)


def test_truncated_completed_exec_retains_session_for_log_recovery() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    result = registry.execute(
        "exec",
        {
            "command": "python -c 'print(\"x\" * 120000)'",
            "yield_time_ms": 2_000,
        },
        context,
    )
    assert result.data["status"] == "completed"
    assert result.data["truncated"] is True
    assert len(result.data["output"]) == 100_000
    assert result.data["session_id"]
    log = registry.execute(
        "process",
        {"action": "log", "session_id": result.data["session_id"], "limit": 1},
        context,
    )
    assert len(log.data["output"]) == 100_000
    assert log.data["truncated"] is True
    backend.delete(sandbox)


def test_backend_capture_limit_is_reported_not_silent(monkeypatch) -> None:
    import mac_bench.runtime.sandbox.memory as memory_module

    monkeypatch.setattr(memory_module, "_MAX_PROCESS_OUTPUT_BYTES", 1_024)
    backend, sandbox, context = _memory_process_context()
    result = phase5_tool_registry().execute(
        "exec",
        {
            "command": "python -c 'print(\"z\" * 3000)'",
            "yield_time_ms": 2_000,
        },
        context,
    )
    assert result.data["status"] == "completed"
    assert result.data["truncated"] is True
    assert len(result.data["output"]) == 1_024
    assert result.data["session_id"]
    backend.delete(sandbox)


def test_process_sessions_expire_and_capacity_is_scoped() -> None:
    backend, sandbox, context = _memory_process_context()
    manager = ProcessManager(retention_seconds=0.05, max_sessions_per_scope=1)
    registry = phase5_tool_registry(manager)
    first = registry.execute(
        "exec", {"command": "sleep .1", "yield_time_ms": 0}, context
    )
    full = registry.execute(
        "exec", {"command": "printf second", "yield_time_ms": 0}, context
    )
    assert full.error["code"] == ToolErrorCode.OUTPUT_LIMIT.value
    registry.execute(
        "process",
        {"action": "poll", "session_id": first.data["session_id"], "wait_ms": 1_000},
        context,
    )
    import time

    time.sleep(0.08)
    assert registry.execute("process", {"action": "list"}, context).data["sessions"] == []
    second = registry.execute(
        "exec", {"command": "printf second", "yield_time_ms": 1_000}, context
    )
    assert second.data["status"] == "completed"
    backend.delete(sandbox)


def test_process_kill_terminates_descendant_and_tty_is_live() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    pty_result = registry.execute(
        "exec",
        {"command": "printf tty-ok", "tty": True, "yield_time_ms": 2_000},
        context,
    )
    assert pty_result.data["status"] == "completed"
    assert "tty-ok" in pty_result.data["output"]

    started = registry.execute(
        "exec",
        {
            "command": "(sleep 1; printf leaked > descendant.txt) & wait",
            "yield_time_ms": 0,
        },
        context,
    )
    killed = registry.execute(
        "process",
        {"action": "kill", "session_id": started.data["session_id"]},
        context,
    )
    assert killed.data["status"] == "killed"
    import time

    time.sleep(1.2)
    assert "workspace/repo/descendant.txt" not in backend._sandboxes[sandbox.id].files
    backend.delete(sandbox)


def test_process_kill_reports_escalation_and_exec_shares_workspace() -> None:
    backend, sandbox, context = _memory_process_context()
    registry = phase5_tool_registry()
    created = registry.execute(
        "exec",
        {"command": "printf shared > generated.txt", "yield_time_ms": 2_000},
        context,
    )
    assert created.data["status"] == "completed"
    assert backend.read_file(sandbox, path="workspace/repo/generated.txt") == b"shared"

    stubborn = registry.execute(
        "exec",
        {
            "command": "python -c 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(\"ready\", flush=True); time.sleep(30)'",
            "yield_time_ms": 100,
        },
        context,
    )
    _wait_for_process_output(registry, context, stubborn, "ready")
    killed = registry.execute(
        "process",
        {"action": "kill", "session_id": stubborn.data["session_id"]},
        context,
    )
    assert killed.data["status"] == "killed"
    assert killed.data["signal"] == "SIGKILL"
    backend.delete(sandbox)
