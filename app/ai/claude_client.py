"""Anthropic client factory and the request options shared by every Claude call.

* Model: Claude Opus 5.5 (``ANTHROPIC_MODEL`` to override).
* Server-side refusal fallback is enabled (``fallbacks: "default"``): if a safety classifier declines a
  request, the API re-runs it on a suitable fallback model inside the same call. A ``refusal`` stop reason
  on the final response therefore means the whole chain declined.
"""

from functools import lru_cache

import anthropic

from app.core.config import get_settings
from app.core.errors import AppError

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIUnavailableError(AppError):
    status_code = 503
    code = "ai_unavailable"


_override: object | None = None


def set_client(client: object | None) -> None:
    """Tests inject a fake client here."""
    global _override
    _override = client


@lru_cache(maxsize=1)
def _real_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)


def get_client() -> anthropic.Anthropic:
    if _override is not None:
        return _override  # type: ignore[return-value]
    key = get_settings().anthropic_api_key
    if not key:
        raise AIUnavailableError(
            "The AI features are not configured on this server (ANTHROPIC_API_KEY missing)."
        )
    return _real_client(key)


def create(**params):
    """``client.beta.messages.create`` with the model, fallback and error mapping applied."""
    client = get_client()
    try:
        return client.beta.messages.create(
            model=get_settings().anthropic_model,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **params,
        )
    except anthropic.AuthenticationError as exc:
        raise AIUnavailableError("The AI provider rejected the server's API key.") from exc
    except anthropic.RateLimitError as exc:
        raise AppError(
            "The assistant is busy. Please try again in a moment.", status_code=429, code="rate_limited"
        ) from exc
    except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
        raise AIUnavailableError("The AI provider is temporarily unavailable.") from exc


def first_text(response) -> str:
    return next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
