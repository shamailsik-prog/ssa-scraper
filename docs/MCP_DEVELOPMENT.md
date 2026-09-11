# ScrapeGraph MCP — development and operator use only

Amendment D-15 §17. The official ScrapeGraph MCP server may be connected to Cursor or Claude
by an engineer to inspect PUBLIC pages, try extraction prompts and schemas, and compare managed
output with the deterministic parser. It is **not** part of the corpus service: production code
calls the `scrapegraph-py` SDK directly (`scraper/extractors/scrapegraph_managed.py`), and nothing
in the running service depends on an MCP client being connected.

## Permitted

- Inspect public court, PakistanCode, assembly and Gazette pages.
- Test the JSON Schemas exported by `scraper/extractors/schemas.py` (`JudgmentExtraction`,
  `StatuteExtraction`, `InstrumentExtraction`, `SearchResultExtraction`).
- Test the fixed prompt templates in `scraper/extractors/prompts.py` against public HTML.
- Compare a managed result with `scraper.extractors.deterministic.extract_judgment_deterministic`.

## Forbidden

- Transmitting any PakistanLawSite authenticated page, result table or judgment text.
- Transmitting cookies, Playwright storage state, session tokens or any `.env` value.
- Storing an API key or firm credential in this repository.
- Wiring MCP into any task, worker, router or Docker service.
- Using MCP to fetch a page that the source's robots.txt or allow-list forbids.

## Setup (engineer's machine, not the repository)

Cursor — `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "scrapegraph": {
      "command": "uvx",
      "args": ["scrapegraph-mcp"],
      "env": { "SGAI_API_KEY": "<your personal key from your password manager>" }
    }
  }
}
```

Claude Desktop — `claude_desktop_config.json`, same block under `mcpServers`.
Claude Code — `claude mcp add scrapegraph -e SGAI_API_KEY=<key> -- uvx scrapegraph-mcp`.

The key lives in the engineer's local MCP configuration or secret manager. The corpus service's own
key (`SGAI_API_KEY` in the server `.env`) is a different credential and is never shared.

## Typical development loop

1. Pick a PUBLIC URL that is inside the source's allow-list (`/admin/sources`).
2. Ask the MCP server to `smartscraper` it with the prompt from `build_prompt("judgment")` and the
   JSON Schema from `JudgmentExtraction.json_schema()`.
3. Run the deterministic parser on the same HTML locally and compare citations, court, date and
   bench fields. Anything the AI returns that is not in the raw text is a validation conflict in
   production (`scraper/extractors/validation.py`), never a corpus value.
4. Adjust the schema or prompt in the repository, bump `SGAI_SCHEMA_VERSION`, and let the cache
   invalidate; never adjust production behaviour from inside an MCP session.
