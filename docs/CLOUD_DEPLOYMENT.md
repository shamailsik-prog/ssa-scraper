# Cloud deployment

The corpus service runs on one Linux server that the firm controls. That server is the
"trusted host" (`ENVIRONMENT=chambers` in `.env`, whatever its physical location): login-session
scraping runs only there, and login-session content, cookies and storage state never leave it.

## 1. Rent a server

Any provider works (Hetzner, DigitalOcean, Vultr, Linode, AWS Lightsail, Contabo, a local Pakistani
host). Choose:

| Item | Minimum | Comfortable |
|---|---|---|
| Image | Ubuntu 24.04 LTS (or 22.04, Debian 12) | same |
| vCPU | 2 | 4 |
| RAM | 4 GB | 8 GB |
| Disk | 80 GB SSD | 160 GB+ (original PDFs accumulate) |
| Region | a deliberate choice; record it in `DEPLOY_REGION` | same |

Open only ports 22, 80 and 443 in the provider's firewall (the installer also configures `ufw`).

Optional but recommended: point a DNS name (for example `corpus.yourfirm.pk`) at the server's IP
before installing. With a name, Caddy obtains a Let's Encrypt certificate automatically. Without
one, the dashboard is served on the IP with a self-signed certificate (encrypted; the browser warns
once).

## 2. Install

### Option A — from GitHub, no terminal (recommended)

The repository carries a workflow that rents the server and installs everything for you.

1. Create a DigitalOcean account and, under **API → Tokens**, generate a personal access token with
   read and write scopes. Copy it once; DigitalOcean will not show it again.
2. In this repository: **Settings → Secrets and variables → Actions → New repository secret**,
   name `DO_TOKEN`, paste the token. Optionally add `SGAI_API_KEY` the same way.
3. **Actions → deploy-cloud → Run workflow.** Choose the region and server size, optionally a DNS
   name, your reporters and earliest year. Press *Run workflow*.
4. After ten to fifteen minutes the run's **Summary** shows the dashboard address and the admin key.

Re-running the workflow updates the code on the same server and keeps its data and `.env`. The
workflow derives its SSH deploy key from `DO_TOKEN`, so no private key is stored anywhere; if you
rotate the token, the next run registers a new key and, for an existing server, you add it once
under the droplet's access settings or recreate the droplet.

### Option B — from a terminal on the server

SSH in as root (or a sudo user).

**If the repository is private** (it is, at the time of writing), GitHub needs a token to hand
out the code. Create one at GitHub → Settings → Developer settings → Fine-grained tokens: repository
access limited to `ssa-scraper`, permission *Contents: Read-only*, expiry as you prefer. Then:

```bash
read -rsp "GitHub token: " GH_TOKEN; echo; export GH_TOKEN
curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
  https://raw.githubusercontent.com/shamailsik-prog/ssa-scraper/main/cloud/install.sh \
  | sudo -E bash -s -- --domain corpus.yourfirm.pk --reporters PLD,SCMR,CLC,PLC,MLD,YLR,PCrLJ --earliest-year 1990 --region Frankfurt
```

The token is used only to download and later update the code; it is never written to disk.

**If the repository is public**, the same command without the token lines:

```bash
curl -fsSL https://raw.githubusercontent.com/shamailsik-prog/ssa-scraper/main/cloud/install.sh \
  | sudo bash -s -- --domain corpus.yourfirm.pk --reporters PLD,SCMR,CLC,PLC,MLD,YLR,PCrLJ --earliest-year 1990 --region Frankfurt
```

All flags are optional. The script:

1. installs Docker, git and a firewall;
2. clones the repository into `/opt/ssa-scraper`;
3. writes `.env` with fresh encryption key, admin key, database and role passwords
   (permissions 600), `ENVIRONMENT=chambers`, `ALLOW_LOGIN_SCRAPING=true`, `API_BIND=127.0.0.1`;
4. asks, on a hidden prompt, for the ScrapeGraph API key (press Enter to add it later);
5. writes the Caddy configuration for your domain or IP;
6. builds the image (Chromium is downloaded into it) and starts all services;
7. prints the dashboard address and the admin key.

The script never asks for PakistanLawSite credentials. Configure those only from the dashboard's
**Human login** tab on the trusted chambers host: either type manually each time, or save slot 1/2
credentials encrypted at rest on the server so they can be reused.

Re-running the same command later (with the token lines again while private) updates the code and restarts the stack; `.env` is kept.

## 3. First use

1. Open the dashboard address, paste the admin key, press **Connect**.
2. **Overview → Not configured**: anything listed there is a value in `/opt/ssa-scraper/.env`
   (`SGAI_API_KEY`, `SGAI_DAILY_CREDIT_CAP`, `OPENAI_API_KEY` for embeddings, and so on). After
   editing: `cd /opt/ssa-scraper && docker compose up -d`.
3. **Human login**: optionally save encrypted credentials for slot 1 and slot 2 (primary/alternate)
   first, then use **Login with saved credentials** to auto-fill (and auto-submit when the slot is
   empty) or run a fully manual login stream. In manual mode, choose slot 1, **Start login**, tap
   the username field in the streamed page, type in the box under the picture (on a phone the
   keyboard opens there; on a computer you can also type straight into the picture), tap the
   password field, type, tick **I Agree with the Terms and Conditions**, tap **Sign in**, then
   **Complete**.

   Security tradeoff: saved credentials reduce operator friction during TLS/IP interruptions but
   keep decryptable credentials on the server. Use this only on a firm-controlled trusted host,
   rotate by overwriting, and clear unused slots.

   PakistanLawSite allows one login per account at a time. If the page shows **Logout From All
   Devices**, the account is still logged in elsewhere (your own browser or phone): enter the
   username and password once more in that box and the site logs you in here instead. Keep this in
   mind while the scraper runs: logging in to the site yourself with the same account ends the
   scraper's session (the slot shows NEEDS_HUMAN_LOGIN and you repeat this step).
4. **Archive storage**: add a target (a bucket, Dropbox, Google Drive, OneDrive, SFTP, SMB or a
   folder on the server). Configuration is encrypted at rest.
5. **Sources**: public courts, PakistanCode, the legislatures and the Gazette run on their own
   schedule. The Supreme Court website's robots.txt disallows its judgment path; that source halts
   for your review as the contract requires.
6. **Backfill mode**: in *Overview*, keep `HARVEST_MODE=backfill` for full-speed initial harvest.
   Backfill defaults are controlled by environment values such as:
   `BACKFILL_PAGES_PER_HOUR`, `BACKFILL_PAGES_PER_DAY`, `BACKFILL_LOGIN_DELAY_MIN/MAX`,
   `BACKFILL_SOURCE_FREQUENCY_MINUTES`, and `BACKFILL_LOGIN_SESSION_CONCURRENCY`.
   When frontier is drained and your target counts are met, switch to `updates` mode from the
   dashboard (or let auto-switch do it) to run selected sources every six hours by default.
7. **ScrapeGraph setup**: if `/admin/scrapegraph/status` shows `SGAI_API_KEY` as NOT CONFIGURED,
   managed/hybrid extraction will not run. Set the key on-server without putting it on a command
   line:
   `printf '%s' "$SGAI_API_KEY" | python3 /opt/ssa-scraper/cloud/set_env.py SGAI_API_KEY`.
   Use the same helper for `SGAI_DAILY_CREDIT_CAP` and `SGAI_PUBLIC_TEST_URL`.
   Then recreate runtime containers:
   `cd /opt/ssa-scraper && docker compose up -d --force-recreate api worker-public worker-scraper celery-beat`.

## 4. Operating

```bash
cd /opt/ssa-scraper
docker compose ps                         # health of every service
docker compose logs -f --tail=100         # live logs
docker compose logs worker-public         # one service
docker compose up -d                      # apply .env changes
docker compose down                       # stop (data is kept in Docker volumes)
```

Backups: the database lives in the `pgdata` volume, original documents under `raw/` and `live/`,
encrypted session state in the database. Archive targets are the off-server copy; run
**Reconcile storage** after restoring anything.

## 5. What the overlay changes

`docker-compose.cloud.yml` adds Caddy (ports 80/443) and `API_BIND=127.0.0.1` keeps port 8000 off
the public interface. Everything you see in the dashboard, the admin key, the human-login
screencast and every keystroke you type into it therefore travels only over TLS. Do not run the
service on a public address without the overlay or an equivalent proxy.
