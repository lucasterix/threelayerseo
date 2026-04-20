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

    # ─── Site operator information (Impressum nach TMG §5 DE) ──────────────
    # Filled once in .env; applied to every auto-generated imprint. Per-site
    # overrides go via the admin form on site_detail.
    operator_name: str = ""
    operator_address: str = ""           # "Musterstraße 1, 12345 Musterstadt"
    operator_email: str = ""
    operator_phone: str = ""
    operator_tax_id: str = ""            # e.g. "DE123456789" (optional)
    operator_contact_url: str = ""       # used for DSGVO contact line

    # ─── OpenAI Images (featured images) ────────────────────────────────────
    # dall-e-3 standard 1024x1024 = $0.040 / image, dall-e-2 = $0.020.
    # gpt-image-1 (low quality) is even cheaper at ~$0.011 if account has access.
    openai_image_model: str = "dall-e-3"
    openai_image_quality: str = "standard"   # or "hd"
    openai_image_size: str = "1024x1024"
    # Where the worker writes PNGs; renderer serves from the same volume.
    images_dir: str = "/srv/app/images"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
