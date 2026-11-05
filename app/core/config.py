"""Application settings, read from environment variables only (never from committed files)."""

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="development", description="development | test | production")
    database_url: str = Field(default="sqlite:///./lumiere-dev.db")
    # Comma-separated in the environment (NoDecode: not parsed as JSON).
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Identity provider: "privy" (email-code accounts, default) or "supabase".
    auth_provider: str = "privy"
    privy_app_id: str = ""
    privy_app_secret: str = ""  # server-side only: profile email lookup and account deletion

    # Supabase Auth. The backend only needs public verification material plus, optionally,
    # the service-role key for server-side account deletion. The service-role key must never
    # be exposed to a browser.
    supabase_url: str = ""
    supabase_jwt_audience: str = "authenticated"
    supabase_jwks_url: str = ""  # defaults to {supabase_url}/auth/v1/.well-known/jwks.json
    supabase_jwt_secret: str = ""  # legacy HS256 projects only
    supabase_service_role_key: str = ""

    # External data
    fred_api_key: str = ""
    eurostat_hicp_dataset: str = "prc_hicp_minr"
    economic_data_ttl_hours: int = 24
    http_timeout_seconds: float = 15.0
    http_max_retries: int = 3

    # Anthropic (transaction classification fallback and the agent)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    agent_max_tool_rounds: int = 6

    # Limits
    max_upload_bytes: int = 5 * 1024 * 1024
    max_csv_rows: int = 50_000
    rate_limit_per_minute: int = 120
    agent_rate_limit_per_minute: int = 12

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def auth_configured(self) -> bool:
        return bool(self.privy_app_id) if self.auth_provider == "privy" else bool(self.supabase_url)

    @property
    def jwks_url(self) -> str:
        if self.auth_provider == "privy":
            return f"https://auth.privy.io/api/v1/apps/{self.privy_app_id}/jwks.json"
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json" if self.supabase_url else ""

    @property
    def jwt_issuer(self) -> str:
        if self.auth_provider == "privy":
            return "privy.io"
        return f"{self.supabase_url.rstrip('/')}/auth/v1" if self.supabase_url else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
