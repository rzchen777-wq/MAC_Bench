from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Mapping


def _resolve_env_ref(value: Any, environ: Mapping[str, str]) -> str | None:
    """Resolve ``${VAR}`` / ``$VAR``; pass through plain strings; ``None`` stays empty."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("${") and text.endswith("}"):
        return environ.get(text[2:-1].strip()) or None
    if text.startswith("$") and len(text) > 1 and text[1:].replace("_", "").isalnum():
        return environ.get(text[1:]) or None
    return text


@dataclass(frozen=True, repr=False)
class LLMConfig:
    """Connection settings for an OpenAI-compatible chat-completions API."""

    base_url: str
    api_key: str | None
    model: str
    timeout_seconds: float = 120.0
    max_retries: int = 3
    thinking_mode: Literal["enabled", "disabled"] | None = "disabled"
    image_input: bool = False

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_api_key: bool = False,
    ) -> "LLMConfig":
        return cls.from_mapping(None, environ=environ, require_api_key=require_api_key)

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any] | None,
        environ: Mapping[str, str] | None = None,
        *,
        require_api_key: bool = False,
    ) -> "LLMConfig":
        """Build config from method YAML ``llm:`` plus process env.

        Precedence for each field: non-empty YAML value → env → default.
        Secrets should use ``api_key_env`` (or ``${VAR}``) rather than literals.
        """

        if environ is None:
            try:
                from dotenv import load_dotenv

                load_dotenv()
            except ImportError:
                pass
        env = os.environ if environ is None else environ
        m = dict(mapping or {})

        base_url = (
            _resolve_env_ref(m.get("base_url"), env)
            or env.get("MAC_BENCH_LLM_BASE_URL")
            or env.get("OPENAI_BASE_URL")
            or env.get("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        ).rstrip("/")

        api_key = _resolve_env_ref(m.get("api_key"), env)
        if not api_key:
            key_env = str(m.get("api_key_env") or "").strip()
            if key_env:
                api_key = env.get(key_env) or None
        if not api_key:
            api_key = (
                env.get("MAC_BENCH_LLM_API_KEY")
                or env.get("OPENAI_API_KEY")
            )

        model = (
            _resolve_env_ref(m.get("model_name") or m.get("model"), env)
            or env.get("MAC_BENCH_LLM_MODEL")
            or env.get("OPENAI_MODEL")
            or ""
        )

        timeout_raw = m.get("timeout_seconds")
        if timeout_raw is None:
            timeout_raw = env.get("MAC_BENCH_LLM_TIMEOUT_SECONDS", "120")
        retries_raw = m.get("max_retries")
        if retries_raw is None:
            retries_raw = env.get("MAC_BENCH_LLM_MAX_RETRIES", "3")
        thinking_raw = m.get("thinking")
        if thinking_raw is None:
            thinking_raw = env.get("MAC_BENCH_LLM_THINKING", "disabled")
        thinking_raw = str(thinking_raw).strip().lower()
        image_input_raw = m.get("image_input")
        if image_input_raw is None:
            image_input_raw = env.get("MAC_BENCH_LLM_IMAGE_INPUT", "false")

        if not base_url.startswith(("http://", "https://")):
            raise ValueError("LLM base URL must start with http:// or https://")
        if require_api_key and not api_key:
            raise RuntimeError(
                "No LLM API key configured. Set llm.api_key_env in "
                "configs/baselines/*.yaml and the matching env var, or "
                "MAC_BENCH_LLM_API_KEY / OPENAI_API_KEY."
            )
        if not model:
            raise RuntimeError(
                "No LLM model configured. Set llm.model_name in "
                "configs/baselines/*.yaml or MAC_BENCH_LLM_MODEL / OPENAI_MODEL."
            )
        try:
            timeout_seconds = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("llm.timeout_seconds must be numeric") from exc
        if timeout_seconds <= 0:
            raise ValueError("llm.timeout_seconds must be positive")
        try:
            max_retries = int(retries_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("llm.max_retries must be an integer") from exc
        if max_retries < 0 or max_retries > 10:
            raise ValueError("llm.max_retries must be between 0 and 10")
        if thinking_raw in {"", "disabled", "off", "false", "0", "none"}:
            thinking_mode: Literal["enabled", "disabled"] | None = "disabled"
        elif thinking_raw in {"enabled", "on", "true", "1"}:
            thinking_mode = "enabled"
        else:
            raise ValueError("llm.thinking must be 'disabled' or 'enabled'")
        if isinstance(image_input_raw, bool):
            image_input = image_input_raw
        else:
            normalized_image_input = str(image_input_raw).strip().lower()
            if normalized_image_input in {"1", "true", "yes", "on", "enabled"}:
                image_input = True
            elif normalized_image_input in {"", "0", "false", "no", "off", "disabled"}:
                image_input = False
            else:
                raise ValueError("llm.image_input must be boolean")

        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            thinking_mode=thinking_mode,
            image_input=image_input,
        )

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def __repr__(self) -> str:
        key = "<configured>" if self.api_key else "<missing>"
        return (
            "LLMConfig("
            f"base_url={self.base_url!r}, api_key={key}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"thinking_mode={self.thinking_mode!r}, image_input={self.image_input!r})"
        )
