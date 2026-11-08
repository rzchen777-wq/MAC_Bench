from __future__ import annotations

import unittest

from mac_bench.runtime.llm.config import LLMConfig


class LLMConfigTest(unittest.TestCase):
    def test_benchmark_variables_take_precedence_and_key_is_redacted(self) -> None:
        config = LLMConfig.from_env(
            {
                "MAC_BENCH_LLM_BASE_URL": "https://gateway.example/v1/",
                "MAC_BENCH_LLM_API_KEY": "top-secret",
                "MAC_BENCH_LLM_MODEL": "example-model",
                "OPENAI_BASE_URL": "https://ignored.example/v1",
                "OPENAI_API_KEY": "ignored-secret",
                "OPENAI_MODEL": "ignored-model",
            },
            require_api_key=True,
        )

        self.assertEqual("https://gateway.example/v1", config.base_url)
        self.assertEqual(
            "https://gateway.example/v1/chat/completions",
            config.chat_completions_url,
        )
        self.assertEqual("example-model", config.model)
        self.assertEqual(3, config.max_retries)
        self.assertEqual("disabled", config.thinking_mode)
        self.assertNotIn("top-secret", repr(config))
        self.assertNotIn("ignored-secret", repr(config))

    def test_unrelated_baseline_variables_do_not_select_the_connection(self) -> None:
        env = {
            "DEV_API_BASE": "https://unrelated.example/v1",
            "DEV_API_KEY": "unrelated-key",
            "META_TEAM_MODEL": "unrelated-model",
            "OPENAI_BASE_URL": "https://selected.example/v1",
            "OPENAI_API_KEY": "selected-key",
            "OPENAI_MODEL": "selected-model",
        }
        config = LLMConfig.from_env(env, require_api_key=True)
        self.assertEqual("https://selected.example/v1", config.base_url)
        self.assertEqual("selected-key", config.api_key)
        self.assertEqual("selected-model", config.model)
        with self.assertRaisesRegex(RuntimeError, "No LLM API key configured"):
            LLMConfig.from_env({key: value for key, value in env.items()
                               if not key.startswith("OPENAI_")}, require_api_key=True)

    def test_dry_configuration_can_omit_key(self) -> None:
        config = LLMConfig.from_env(
            {"MAC_BENCH_LLM_MODEL": "fake-model"}, require_api_key=False
        )
        self.assertIsNone(config.api_key)
        self.assertIn("<missing>", repr(config))

    def test_live_configuration_requires_key(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "No LLM API key configured"):
            LLMConfig.from_env(
                {"MAC_BENCH_LLM_MODEL": "fake-model"}, require_api_key=True
            )


if __name__ == "__main__":
    unittest.main()
