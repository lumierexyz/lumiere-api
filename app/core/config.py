"""Application settings, read from environment variables only (never from committed files)."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="development", description="development | test | production")
    database_url: str = Field(default="sqlite:///./lumiere-dev.db")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

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

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
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
    def jwks_url(self) -> str:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json" if self.supabase_url else ""

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1" if self.supabase_url else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
