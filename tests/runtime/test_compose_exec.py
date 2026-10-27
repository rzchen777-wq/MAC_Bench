"""Compose-app backend path mapping and docker-exec wrapping."""

from __future__ import annotations

import unittest

from mac_bench.runtime.sandbox.base import CommandResult, SandboxHandle
from mac_bench.runtime.sandbox.compose_exec import (
    DockerComposeAppBackend,
    map_app_path_to_host,
)


class MapAppPathTest(unittest.TestCase):
    def test_relative_and_absolute_under_app(self) -> None:
        self.assertEqual(
            map_app_path_to_host("src/main.rs"),
            "/work/sandbox/docker/workspace/src/main.rs",
        )
        self.assertEqual(
            map_app_path_to_host("/app/Cargo.toml"),
            "/work/sandbox/docker/workspace/Cargo.toml",
        )
        self.assertEqual(
            map_app_path_to_host("."),
            "/work/sandbox/docker/workspace",
        )

    def test_rejects_escape(self) -> None:
        with self.assertRaises(ValueError):
            map_app_path_to_host("../etc/passwd")
        with self.assertRaises(ValueError):
            map_app_path_to_host("/problem/task.md")


class RecordingHost:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def exec(self, sandbox, command, *, cwd=None, env=None, timeout=None):
        del sandbox, cwd, env, timeout
        self.commands.append(command)
        return CommandResult(command, 0, "/app", 0.01)

    def write_file(self, sandbox, *, path, content):
        del sandbox, path, content

    def read_file(self, sandbox, *, path):
        del sandbox, path
        return b""

    def start_process(self, sandbox, *, session_id, command, cwd, tty):
        del sandbox, cwd, tty
        self.commands.append(command)
        return type("H", (), {"id": session_id, "raw": object()})()


class DockerComposeAppBackendTest(unittest.TestCase):
    def test_exec_wraps_docker_exec_into_named_container(self) -> None:
        host = RecordingHost()
        backend = DockerComposeAppBackend(host, container="app_ubsmhopy")
        sandbox = SandboxHandle(id="sb", provider="modal", raw=object())
        backend.exec(sandbox, "ls -la", cwd="/app", timeout=30)
        self.assertEqual(len(host.commands), 1)
        command = host.commands[0]
        self.assertIn("docker exec", command)
        self.assertIn("app_ubsmhopy", command)
        self.assertIn("-w /app", command)
        self.assertNotIn("docker exec", command.split("bash -lc", 1)[-1])


if __name__ == "__main__":
    unittest.main()
