# ScrapeGraphAI + Playwright package for SIKANDER AI Corpus Service

This branch is the base branch for the ScrapeGraphAI integration work.

## Primary command

`SIKANDER_AI_SCRAPER_SCRAPEGRAPH_INTEGRATED_MASTER_COMMAND.md` is the single autonomous build instruction for Cursor Background Agent / Claude Code. It preserves the Pakistani legal corpus architecture and adds ScrapeGraphAI as a controlled extraction layer while Playwright remains responsible for browser navigation, authenticated sessions, JavaScript forms and downloads.

## Existing corpus command

`CURSOR_COMMAND_SCRAPER_REPO.md` is retained as the original short launcher/acceptance command.

## Required governing source documents

The full corpus build remains governed by `FINAL_SCRAPER_PROMPT_3.md`. The operator/UX reference is `SIKANDER_AI_Scraper_Plain_English_11Sep2026.pdf`. Those documents should be kept at repository root when available to the coding agent. The integrated master command expressly treats the full corpus prompt as governing and overrides it only where the ScrapeGraphAI amendment says so.

## Architecture

- Playwright: login, session storage state, JS navigation, search forms, pagination and browser-only downloads.
- HTTPX: public/static requests, APIs and binary/PDF downloads.
- Deterministic legal parsers: Pakistani reporter citations, statute patterns, bench/date validation, deduplication and final validation.
- ScrapeGraphAI managed API: public-source structured extraction only.
- ScrapeGraphAI local/self-hosted: optional private/local extraction for material that may not leave firm infrastructure.
- PostgreSQL 16 + pgvector + pg_trgm: canonical working corpus.
- Celery + Redis: 24/7 scheduling and workers.
- Archive mirrors: Google Drive, Dropbox, OneDrive, S3-compatible, SFTP, SMB and local path.

## Critical privacy rule

Authenticated `login_session` material, cookies, passwords, Playwright storage state and PakistanLawSite full text must never be sent to the managed ScrapeGraphAI API, remote ScrapeGraph MCP, or another third-party LLM. The managed service is for approved public sources only. Login-session extraction must use deterministic parsing and/or a local/self-hosted extraction engine.

## MCP

MCP is optional development/operator tooling. Production scraping must run from the Python/Celery corpus service and must not depend on Claude, Cursor or an MCP client remaining open.

## Secrets

Never commit real credentials. Populate environment variables through the deployment secret manager or `.env` ignored by Git. Use `scrapegraph.env.example` only as a template.

## Branch

`claude/scrapegraphai-mcp-plugins-0dh85u`
