# AI Order Parser — Production Runbook

Server: `187.126.114.34` · App: `/opt/aiparser` · Public URL: `https://aiparser.tech`
Repo: `https://github.com/yashmakwana61/aiparser.git` (branch `main`)

## Deploy

```bash
ssh root@187.126.114.34
cd /opt/aiparser
git pull --ff-only          # must show Fast-forward, no local changes expected
docker compose up -d --build parser
sleep 25
curl -s http://127.0.0.1:8000/ready
```

Single worker is mandatory (`--workers 1` in Dockerfile): queue, sessions
and Telegram polling are in-process. Never scale workers.

## Health checks (point uptime monitoring here)

| Check | URL | Healthy when |
|---|---|---|
| Liveness | `https://aiparser.tech/health` | `{"status":"ok"}` |
| Readiness | `https://aiparser.tech/ready` (header `Authorization: Bearer …` NOT needed) | `"ready":true`, all components `ok`/`configured` |
| Metrics | `https://aiparser.tech/metrics` (auth required) | scrape for `jobs_failed_total`, `orders_*`, queue depth |

Alert on: `/ready` non-200, `job_queue_depth` growing, `jobs_failed_total`
rising, disk > 80% (`df -h /`).

## Secrets (never commit; live in `/opt/aiparser/.env`, chmod 600)

`API_AUTH_TOKEN`, `PUTER_AUTH_TOKEN`, `GOOGLE_VISION_API_KEY`,
`ODOO_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`.
Rotate by editing `.env` then `docker compose up -d parser` (no rebuild needed
for env-only changes).

## Known provider failure modes

| Signal | Meaning | Action |
|---|---|---|
| Telegram: *"AI service out of credit"* (`AI_QUOTA_EXHAUSTED`) | Puter balance empty (HTTP 402) | Top up at puter.com/dashboard; resend orders after |
| `odoo.*_lookup_failed` warnings | Odoo version quirks | Defensive fallbacks handle them; fix only if orders block |
| `alias_target_missing` warnings | Alias points at deleted product/partner | Run `validate-aliases`, deactivate dead ones |
| `telegram.duplicate_job_blocked` | Same file re-sent | By design; point user at the original case |

## Maintenance commands (run on server)

```bash
cd /opt/aiparser
docker compose exec parser python -m order_parser.tools.maintenance validate-aliases
docker compose exec parser python -m order_parser.tools.maintenance validate-aliases --deactivate
docker compose exec parser python -m order_parser.tools.maintenance mine-corrections --min-repeats 2
docker compose exec parser python -m order_parser.tools.maintenance pending-summary
```

## Backup / restore

All state lives in the `parser-logs` Docker volume (`/app/logs`: jobs,
pending, aliases, sessions, audit, uploads) plus `caddy-data` (TLS).
Back up both volumes before upgrades:

```bash
docker run --rm -v aiparser_parser-logs:/src -v /opt/backups:/dst alpine \
  tar czf /dst/parser-logs-$(date +%F).tgz -C /src .
```

Restore = stop compose, extract over the volumes, start compose.

## Scaling notes

- 100+ orders/day is fine on one worker (queue `QUEUE_MAX_SIZE=500`,
  `JOB_TIMEOUT_SECONDS=300`). Watch `job_queue_depth` during bursts.
- Keep `ENABLE_IDEMPOTENCY=true` (cross-restart duplicate protection).
- Keep the retention sweeper on (`SESSION_RETENTION_DAYS=30`); audit kept forever.
- Restrict the bot with `AUTHORIZED_STAFF="id:name,..."` once staff IDs are known.
- Catalog cache TTL is 30 min; brand-new Odoo products are picked up on
  the next refresh or automatically on a resolution miss (refresh-on-miss).
