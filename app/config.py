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

    # IP address blog domains resolve to. Used when configuring INWX DNS after
    # a purchase. Override with SERVER_IP in .env if the renderer moves.
    server_ip: str = "46.224.7.46"

    # IndexNow key (hex string). Must be reachable at
    # https://<domain>/<key>.txt for every site — the renderer serves this
    # automatically. Generate once with: python -c "import secrets;
    # print(secrets.token_hex(16))".
    indexnow_key: str = ""

    # DataForSEO (keyword research / SERPs / volumes). Basic auth =
    # login:password. Leave empty to disable the keyword tool.
    dataforseo_login: str = ""
    dataforseo_password: str = ""

    # Google Search Console via Service Account. Path to the JSON key the
    # user downloads from GCP IAM -> Service Accounts -> Keys. Mounted
    # read-only into the admin + worker containers; empty string disables
    # the integration cleanly.
    google_credentials_path: str = "/run/secrets/google-sa.json"

    # Cloudflare API token (scope: Zone:Edit, DNS:Edit). Used to add
    # domains as CF zones and flip the proxy ("orange cloud") on — that's
    # how we get IP diversity without buying a second host.
    cloudflare_api_token: str = ""

    # Optional separate read-scope token (Account:Read + Zone:Read). If
    # unset the main token is reused — works as long as it has read rights.
    cloudflare_read_token: str = ""

    # Cloudflare account UUID. Shown in the dashboard URL after login:
    # dash.cloudflare.com/<ACCOUNT_ID>/. Required to create zones.
    cloudflare_account_id: str = ""

    # Hetzner Cloud API token, for future auto-provisioning of new
    # servers when the fleet hits soft-capacity limits.
    hetzner_api_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
