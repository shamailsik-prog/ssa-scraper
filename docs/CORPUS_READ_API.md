# Read-only corpus API (`/corpus/v1`)

This API serves the Sikander legal research and drafting agent. The agent's design is in
`cursor-init` under `docs/agents/sikander-legal-agent/agent_spec.md`. It gives the agent the five reads its
tools need. Nothing in it writes to the database.

| Endpoint | Agent tool | What it returns |
|---|---|---|
| `GET /corpus/v1/judgments/search?q=&court=&year_from=&year_to=&limit=` | `search_judgments` | Judgments whose citation, title, headnotes or full text contain every word of `q`, newest first, with a snippet (at most 50) |
| `GET /corpus/v1/judgments/by-citation?citation=` | `get_judgment` | One judgment with its full text and bench, found by its canonical citation or any of its parallel citations; 404 when not held |
| `GET /corpus/v1/citations/verify?citation=` | `verify_citation` | `found: true` with the canonical citation, court, year and parallel citations, or `found: false`; it never guesses |
| `GET /corpus/v1/judgments/citing?citation=&limit=` | `find_citing_judgments` | Later judgments citing it, each with its recorded treatment, confidence, evidence passage and whether a person has reviewed it. An empty list means no treatment is recorded, not that the case is good law |
| `GET /corpus/v1/statutes/section?statute=&section=` | `get_statute_section` | The current text of one section, with version number, effective date and version confidence. The statute is matched by short name (e.g. `CrPC`) or by the words of its name |

## Access and safety

- **Its own key.** Every request sends `X-Corpus-Key: <CORPUS_API_KEY>`. The admin key does not work here,
  so the agent never holds admin power. Repeated wrong keys from one address are refused for five minutes.
  They are counted separately from wrong admin keys, so a misconfigured agent cannot lock you out of `/admin`.
- **Its own login.** Every query runs as the `corpus_writer` role in a read-only transaction with a
  15-second time limit, so a heavy search cannot hold the database. Direct SQL uses `sikander_reader`,
  which has column-level SELECT only on content tables (no `full_text` / staging blobs); see
  `docs/CHANGES.md`.
- **Off by default.** With `CORPUS_API_KEY` blank, the API answers `503 not configured`. It also needs
  `CORPUS_WRITER_PASSWORD`, which every install sets.

## Turning it on

- **Fresh servers:** `cloud/install.sh` generates a random `CORPUS_API_KEY`, so the API is on from the start.
- **The existing server:**
  1. Add a repository secret named `CORPUS_API_KEY`, for example from `openssl rand -hex 32`.
  2. Run the `deploy-cloud` workflow. It writes the key to the server's `.env` over stdin, never printing it,
     and restarts the services.
  3. Give the agent the same key as its `SIKANDER_CORPUS_API_KEY` secret.

## Known limits

- **Search scans text.** It checks headnotes and full text with `ILIKE`, not a full-text index, so a broad
  query over a large corpus can hit the 15-second limit. Narrow it by court or year. A full-text index is
  the next step if this becomes common.
- **The certificate may not be trusted yet.** A server reached by IP address, without a domain, uses Caddy's
  own certificate, which outside callers such as DataRobot will not trust. Point a domain at the server, so
  Caddy obtains a Let's Encrypt certificate, before the agent calls this API from outside.
