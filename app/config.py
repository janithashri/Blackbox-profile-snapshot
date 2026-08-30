from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    LINKEDIN_LI_AT: str = ""
    LINKEDIN_JSESSIONID: str = ""
    LINKEDIN_LI_AT_PRIMARY: str = ""
    LINKEDIN_JSESSIONID_PRIMARY: str = ""
    LINKEDIN_LI_AT_SECONDARY: str = ""
    LINKEDIN_JSESSIONID_SECONDARY: str = ""
    ENVIRONMENT: str = "development"
    LINKEDIN_HTTP_PROXY: str = ""
    LINKEDIN_PROXY_INSECURE: bool = False
    LINKEDIN_CAPTURE_DIR: str = ""
    LINKEDIN_SESSION_HEALTH_LOG: str = "data/session_health.jsonl"
    LINKEDIN_MIN_INTERVAL_SECONDS: float = 2.0
    LINKEDIN_CACHE_TTL_SECONDS: float = 300.0
    LINKEDIN_MAX_INFLIGHT_JOBS: int = 3


settings = Settings()
