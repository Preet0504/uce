import os


class LLMClientError(RuntimeError):
    pass


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    value = value.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    cleaned = value.strip().lower()
    if cleaned in {"1", "true", "yes", "y", "on"}:
        return True
    if cleaned in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _is_placeholder(value: str, placeholder: str) -> bool:
    return not value or value.strip() == placeholder


class LLMClient:
    def __init__(self) -> None:
        self.primary = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
        self.fallback = (os.getenv("LLM_FALLBACK") or "openai").strip().lower()

        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "<CLAUDE_API_KEY_PLACEHOLDER>")
        self.openai_key = os.getenv("OPENAI_API_KEY", "<OPENAI_API_KEY_PLACEHOLDER>")
        self.local_api_key = (
            os.getenv("LOCAL_LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or "local"
        ).strip()

        self.anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.local_model = os.getenv("LOCAL_LLM_MODEL", "gemma4:e4b").strip()
        self.local_base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1").strip()
        self.max_tokens = _env_int("LLM_MAX_TOKENS", 2048)
        self.force_json_response = _env_bool("LLM_FORCE_JSON_RESPONSE", True)

    def generate(self, prompt: str, provider: str | None = None) -> str:
        provider = (provider or self.primary).strip().lower()
        if provider == "anthropic":
            return self._call_anthropic(prompt)
        if provider == "openai":
            return self._call_openai(prompt)
        if provider == "local":
            return self._call_local(prompt)
        raise LLMClientError(f"Unsupported LLM provider: {provider}")

    def _call_anthropic(self, prompt: str) -> str:
        if _is_placeholder(self.anthropic_key, "<CLAUDE_API_KEY_PLACEHOLDER>"):
            raise LLMClientError("ANTHROPIC_API_KEY is not set for the Anthropic provider.")
        try:
            from anthropic import Anthropic
        except Exception as exc:  # pragma: no cover - import dependent
            raise LLMClientError("Anthropic client library is not installed.") from exc

        client = Anthropic(api_key=self.anthropic_key)
        response = client.messages.create(
            model=self.anthropic_model,
            max_tokens=self.max_tokens,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        content = getattr(response, "content", None)
        if isinstance(content, list):
            parts = []
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            return "".join(parts).strip()
        if isinstance(content, str):
            return content.strip()
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text.strip()
        raise LLMClientError("Anthropic response did not contain text output.")

    def _call_openai(self, prompt: str) -> str:
        if _is_placeholder(self.openai_key, "<OPENAI_API_KEY_PLACEHOLDER>"):
            raise LLMClientError("OPENAI_API_KEY is not set for the OpenAI provider.")
        return self._call_openai_compatible(
            prompt=prompt,
            model=self.openai_model,
            api_key=self.openai_key,
            base_url=None,
            provider_name="OpenAI",
        )

    def _call_local(self, prompt: str) -> str:
        if not self.local_base_url:
            raise LLMClientError("LOCAL_LLM_BASE_URL is not set for the local provider.")

        # Local OpenAI-compatible servers typically accept any non-empty key.
        api_key = self.local_api_key
        if _is_placeholder(api_key, "<OPENAI_API_KEY_PLACEHOLDER>"):
            api_key = "local"

        return self._call_openai_compatible(
            prompt=prompt,
            model=self.local_model,
            api_key=api_key,
            base_url=self.local_base_url,
            provider_name="Local",
        )

    def _call_openai_compatible(
        self,
        prompt: str,
        model: str,
        api_key: str,
        base_url: str | None,
        provider_name: str,
    ) -> str:
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - import dependent
            raise LLMClientError("OpenAI-compatible client library is not installed.") from exc

        client = OpenAI(api_key=api_key, base_url=base_url)
        request: dict[str, object] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.force_json_response:
            # Best-effort JSON mode for extraction reliability.
            request["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**request)
        except Exception as exc:
            if self.force_json_response and "response_format" in request:
                # Some local OpenAI-compatible servers may not support response_format.
                request.pop("response_format", None)
                try:
                    response = client.chat.completions.create(**request)
                except Exception as retry_exc:
                    raise LLMClientError(f"{provider_name} request failed: {retry_exc}") from retry_exc
            else:
                raise LLMClientError(f"{provider_name} request failed: {exc}") from exc

        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMClientError(f"{provider_name} response did not include any choices.")
        message = choices[0].message
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                text: str | None = None
                if isinstance(item, dict):
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        text = text_value
                else:
                    text_value = getattr(item, "text", None)
                    if isinstance(text_value, str):
                        text = text_value
                if text:
                    parts.append(text)
            if parts:
                return "".join(parts).strip()

        raise LLMClientError(f"{provider_name} response did not contain text output.")
