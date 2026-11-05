"""Thin factory around the OpenAI SDK so tests can inject a fake client."""

from functools import lru_cache

from openai import OpenAI

from app.core.config import get_settings
from app.core.errors import AppError


class AIUnavailableError(AppError):
    status_code = 503
    code = "ai_unavailable"


_override: object | None = None


def set_client(client: object | None) -> None:
    global _override
    _override = client


@lru_cache(maxsize=1)
def _real_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key, timeout=90, max_retries=2)


def get_client() -> OpenAI:
    if _override is not None:
        return _override  # type: ignore[return-value]
    key = get_settings().openai_api_key
    if not key:
        raise AIUnavailableError(
            "The AI features are not configured on this server (OPENAI_API_KEY missing)."
        )
    return _real_client(key)
