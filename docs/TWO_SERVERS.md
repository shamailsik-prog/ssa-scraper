# Two servers: PakistanLawSite and the free sites

PakistanLawSite and the free sites (courts' websites, PakistanCode, assemblies, gazettes) used to share
one server: one database, one set of queues and workers. Trouble on one held up the other. They now run
as **two servers from the same code**, each with its own database, queues, workers and Drive transfer.

| Server | `DEPLOY_ROLE` | Harvests |
|---|---|---|
| `ssa-corpus` | `pls` | PakistanLawSite only (login, citation grid, search harvest, case-ID walk) |
| `ssa-corpus-public` | `public` | Every free source: court websites (unreported judgments), PakistanCode, legislatures, gazettes |

`DEPLOY_ROLE=all` (the default) keeps the old single-server behaviour, so nothing changes on a server
until its role is set.

## What the role switch does

- **Scheduler:** a server dispatches only its own sources. Beat drops the other server's entries
  (`scraper/deploy_role.py` lists them): a free-sites server never signs in to PakistanLawSite, and the
  PakistanLawSite server never re-fetches statutes from PakistanCode.
- **Stray jobs:** a job for the other server's source is refused, whether it comes from the queue or
  from "Run now" on the dashboard (HTTP 409, naming the other server).
- **Status:** a free-sites server reports PakistanLawSite as `not_harvested_on_this_server` instead of
  "stalled", so its alarms mean something.
- **Everything else stays on both:** promotion, the Drive transfer, embeddings, treatment. Each server
  keeps its own corpus moving.

Both servers write documents in the same format (`docs/CORPUS_RECORD_FORMAT.md`), so the ingestion stage
reads them alike.

## Retired sources

`RETIRED_SOURCES` (default `NasirLawSite,BalochistanHighCourt`) lists sources that are switched off on
every start-up: their rows are set to DISABLED and never dispatched. Both sites block this service (HTTP
403 / access denied since September 2026), and the owner asked for them to be removed. Their records
already collected stay in the database and on Drive. To bring one back, remove it from the setting in
`/opt/ssa-scraper/.env`, restart, and set the source ACTIVE on the dashboard.

## Starting the free-sites server

GitHub → Actions → **deploy-cloud** → Run workflow:

1. `droplet_name`: `ssa-corpus-public`
2. `deploy_role`: `public`
3. the rest as for the main server (region, size; reporters are ignored on this server)

This creates a second DigitalOcean droplet (a recurring monthly cost; `s-2vcpu-4gb` is the default size).
Then connect Google Drive on the new server's dashboard, as on the main one.

## Switching the main server to PakistanLawSite only

Only once the free-sites server is collecting: run **deploy-cloud** again with `droplet_name`
`ssa-corpus` and `deploy_role` `pls`. Doing it in this order means no source goes unharvested in between.

Public judgments and statutes the main server already holds stay in its database and on Drive; nothing
is deleted. The free-sites server collects the public sources afresh into its own database.

When it reaches a judgment whose Drive folder the main server already wrote, it keeps that folder exactly
as it is (Drive files are written once and never overwritten). The PDF and text match and are adopted;
`metadata.json` carries the other server's record number, so the storage reconcile lists it as a
"mismatch". That entry is expected and harmless during the switch-over.
