# ThreeLayerSEO

SEO-Automatisierung mit gestaffelter Backlink-Topologie (Tier 1 → 2 → 3 → Money-Site).
Kauft Domains über INWX in Bulk, baut pro Domain eine generierte Blog-Seite und
befüllt sie mit KI-generierten Artikeln nach einem zweistufigen Pipeline-Prinzip
(OpenAI Deep Research → Claude Writer).

**Live**: https://seo.zdkg.de (Admin)
**Repo**: https://github.com/lucasterix/threelayerseo
**Deploy-Target**: Hetzner `deploy@46.224.7.46`, Caddy als Reverse-Proxy, Docker Compose.

## Architektur

```
 ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
 │  admin (UI)  │     │  renderer    │     │   worker     │
 │ seo.zdkg.de  │     │ alle Blogs   │     │ RQ-Jobs      │
 └──────┬───────┘     └──────┬───────┘     └──────┬───────┘
        └────────────────────┼────────────────────┘
                    ┌────────▼────────┐
                    │ Postgres + Redis│
                    └─────────────────┘
```

- **admin** — FastAPI+HTMX+Tailwind, Dashboard, Domain-Portfolio, Redaktionsplan.
- **renderer** — FastAPI, Multi-Tenant via Host-Header, Theme pro Tier.
- **worker** — RQ-Worker für Domain-Käufe, Content-Generation, Publishing.

## Tiers

| Tier | Qualität       | Voice                           | Backlink-Fluss          |
|------|----------------|---------------------------------|-------------------------|
| 1    | PBN / billig   | Kurz, Keyword-fokussiert        | → Tier 2 (selten Tier 3)|
| 2    | Mittel         | Solide, 2-3 Quellen             | → Tier 3                |
| 3    | Authoritative  | Lang, poliert                   | → Money-Site            |

## Lokale Entwicklung

```bash
cp .env.example .env         # API-Keys eintragen
docker compose up --build
open http://localhost:8100   # Admin (HTTP Basic: admin/admin)
open http://localhost:8101   # Renderer
```

Der Admin-Container legt beim ersten Start die Tabellen via `Base.metadata.create_all` an.

## Deploy

Push auf `main` → GitHub Actions baut Image (`ghcr.io/lucasterix/threelayerseo`)
→ SSH auf Server → `docker compose up -d` in `~/apps/threelayerseo/`.

Required GitHub secrets:
- `DEPLOY_SSH_KEY` — Private Key, Public-Teil in `deploy@46.224.7.46:~/.ssh/authorized_keys`
- `GHCR_READ_TOKEN` — PAT mit `read:packages`

Caddy muss auf dem Host das include-Dir kennen (siehe `deploy/nginx/seo.zdkg.de.caddy`).

## Secrets zum Setzen

In `~/apps/threelayerseo/.env` auf dem Server:
- `INWX_USER`, `INWX_PASSWORD`, `INWX_SHARED_SECRET` (2FA-Seed), `INWX_TEST_MODE=false`
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
- `ADMIN_PASSWORD` (starkes)
- `POSTGRES_PASSWORD`, `SECRET_KEY`
