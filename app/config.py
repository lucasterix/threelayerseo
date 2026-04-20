from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    env: str = "dev"
    secret_key: str = "dev-secret-change-me"

    admin_user: str = "admin"
    admin_password: str = "admin"

    database_url: str = "postgresql+asyncpg://threelayerseo:change-me@db:5432/threelayerseo"
    redis_url: str = "redis://redis:6379/0"

    inwx_user: str = ""
    inwx_password: str = ""
    inwx_shared_secret: str = ""
    inwx_test_mode: bool = True

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    admin_host: str = "seo.zdkg.de"
    renderer_default_host: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
