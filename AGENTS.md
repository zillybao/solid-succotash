# AGENTS.md

## Project Overview
A scheduled agent that scans a configured list of company career boards **twice a
day**, detects new internships relevant to embedded/firmware/ASIC/FPGA/RTL work, and
**appends only new rows** to a Google Sheet.

**Stack**: Python 3.11+, `httpx` + `BeautifulSoup` (`lxml` preferred, `html.parser`
fallback), **Google Sheets** via `gspread`, optional Slack webhook, GitHub Actions
cron (`.github/workflows/intern-finder.yml`) or local `python -m src.run`.

There is **no Playwright** and no generic crawler. Almost every live company is an
ATS JSON (or ATS-backed HTML) parser in `src/parse.py`. Fall back to `ats: html`
only when no public API exists.

Sheets is the datastore so you can mark `applied` by hand without fighting a local
CSV. Credentials live in `.env` / `credentials.json` (gitignored), never in the repo.

## Current coverage
- **`config/sites.yaml`**: ~91 companies, all on named ATS parsers (no `html` entries
  in the live list).
- **`config/urls.txt`**: original career-page URL dump (source of truth for *what we
  considered*).
- **`config/urls_skipped.txt`**: companies still needing a dedicated parser (MediaTek,
  Siemens, L3Harris, Blue Origin, etc.). Do not scrape these until an ATS/API is
  identified and `robots.txt`/ToS are checked.

Supported `ats` values: `greenhouse`, `lever`, `ashby`, `workday`, `eightfold`,
`oracle`, `amazon`, `phenom`, `smartrecruiters`, `talentbrew` (alias `smashfly`),
`apple`, `google`, `tesla`, `arm`, `html`.

## Goals
- Poll only the companies listed in `config/sites.yaml`.
- Extract title, canonical link, location, and posting date when the ATS exposes it.
- Match **description body** against `config/keywords.yaml` (not title alone).
- Drop listings whose location is clearly non-US (`config/locations.yaml`).
- Append only *new* postings — never duplicate, never overwrite history, never write
  description text to the sheet.
- Mark previously `open`/`applied` rows `closed` when the link disappears from that
  company’s live intern-titled set.
- Run unattended; a single site failure must not abort the rest. Fail loudly in logs
  (and Slack, if configured).

## Non-Goals
- No auto-apply / form submission.
- No sites that explicitly disallow automated access in `robots.txt` / ToS.
- Not a general-purpose crawler; no URLs except those in `sites.yaml`.
- Do not parallelize all companies in one run (rate limits / bot walls).
- Do not fetch a job’s description unless the **title** already looks like intern /
  co-op (or the list payload already includes description text).

## Architecture

```
config/
  sites.yaml              # companies + ats + board/host/query/facets
  keywords.yaml           # description-body keywords
  locations.yaml          # US vs non-US location filter
  urls.txt                # original URL inventory
  urls_skipped.txt        # not yet parsed
src/
  run.py                  # fetch -> parse -> filter -> dedupe -> sheet
  fetch.py                # sequential httpx client, retry, split delays
  parse.py                # ATS parsers -> list[JobPosting]
  filter.py               # title noise + US location + description keywords
  dedupe.py               # link normalize + seen-hash cache
  sheet.py                # Google Sheets read/append/mark-closed
  models.py               # JobPosting, SHEET_HEADERS, SCHEMA_VERSION
  notify.py               # optional Slack digest
state/
  seen_jobs.json          # local hash cache (gitignored)
  company_runs.json       # per-company run count / first-seen lookback
logs/
  run-YYYY-MM-DD.log
  skipped-YYYY-MM-DD.log  # intern titles dropped by location or keyword filter
tests/
  fixtures/               # saved HTML for generic parser tests
```

Run locally (project root; do not use an empty `.venv`):

```
python -m src.run --dry-run    # fetch + filter, no sheet writes
python -m src.run              # write to Google Sheets
```

`--dry-run` does not persist `state/company_runs.json` or `seen_jobs.json`.

## Methods (how parsing works)
**Prefer the public ATS list API, then intern-title-gate, then description.**

1. **List** jobs from the ATS (JSON). Use intern facets/`query` when the board
   exposes them (Workday `applied_facets`, Eightfold `filter_seniority`, Amazon
   `query: internship`, Apple `team=internships-…`).
2. **Title-filter** with `title_keywords` (default: `intern`, `internship`,
   `co-op`, `coop`) *before* any per-job detail fetch. Greenhouse list payloads
   omit `content`; do **not** detail-fetch the whole board (SpaceX-scale boards
   are thousands of full-time roles). Skip Greenhouse `?content=true` — the
   payload can be huge and time out; slim list + intern-only details is the
   intended path. If list JSON already has a description (Lever, Ashby, Amazon,
   Phenom, some Eightfold), use it and skip the extra call.
3. **Location-filter** against `config/locations.yaml`. Drop if a foreign country
   is named with no US signal; keep US country/state/`City, ST` forms and
   empty/remote/city-only strings. Workday list `locationsText` is often
   city-only (`Hyderabad`); after the intern detail fetch, append
   `jobPostingInfo.country.descriptor` so it becomes `Hyderabad, India` and
   the existing country list can drop it. Do not add a foreign-city denylist.
   If the detail call fails, city-only stays (kept as ambiguous).
4. **Keyword-filter** the description in memory against `config/keywords.yaml`
   (token match, so `asic` does not match `basic`). Strip `description` before
   any sheet/cache write.
5. **Dedupe** on `normalize_link(link)` SHA-256 vs sheet rows ∪ `seen_jobs.json`.
   New rows and closed-status updates are written once at the **end** of the run.

Sites are scanned **sequentially**. Delays in `src/fetch.py`:

- JSON ATS (`get_json` / `post_json`): **0.4s** after the previous request finishes
- HTML/SSR (`get_text`: Apple, Google, TalentBrew job pages): **1.5s**

Timeouts retry 3× (transport/timeout only). HTTP 403/404 fail that site and
continue. Expected wall time is **~15–25 min** per run typical, **~10–15 min**
best, **~35–45 min** if unfaceted Workday catalogs (Broadcom, Leidos, BD, …) are
huge or HTML detail counts spike. GitHub Actions `timeout-minutes: 30` may kill a
slow run — treat that as a constraint when adding boards.

Custom / fragile parsers: **Apple** (SSR hydration JSON), **Google**
(`AF_initDataCallback`), **Tesla** (cua-api; often 403 from datacenter IPs).
Arm in `sites.yaml` uses **TalentBrew**, not the unused sitemap `ats: arm` helper.

## Data Model
Each posting normalizes to:

| field        | type   | notes |
|--------------|--------|--------|
| company      | str    | from config, not scraped |
| title        | str    | job title |
| link         | str    | canonical URL — primary dedupe key |
| location     | str    | optional, best-effort |
| description  | str    | in-memory only for keyword match; **never written to the sheet** |
| status       | str    | `open` / `applied` / `closed` — script sets open/closed; `applied` is manual |
| date_found   | date   | when this run first kept it |
| date_posted  | date   | optional; ISO, epoch, Workday “Posted N Days Ago”, Amazon `"July 29, 2026"` |
| source_page  | str    | `url` from `sites.yaml` (also the key for closed-status checks) |

Sheet columns (`SCHEMA_VERSION = 1`): `company`, `title`, `link`, `location`,
`status`, `date_found`, `date_posted`, `source_page`. Headers live in A–H only;
do not write sentinels outside that table (a value in Z1 made `append_rows`
land in column Z). Pin appends with `table_range="A1"`.

## Spreadsheet Contract
- One row per unique normalized `link`.
- Never delete or reorder rows the agent didn’t add.
- Dedupe key = normalized link (lowercase host, strip tracking params / fragment /
  trailing slash). Hashes live in `state/seen_jobs.json`; the sheet is also read
  each non-dry run.
- New rows append at the bottom.
- Do not silently reshape existing columns — bump `SCHEMA_VERSION`.
- `GOOGLE_SHEET_ID` may be the raw ID or a
  `https://docs.google.com/spreadsheets/d/<id>/...` URL.
- `GOOGLE_SHEET_WORKSHEET` is the tab name. Blank / unset (including an empty
  GitHub Actions secret) falls back to `Sheet1` — Actions always injects the
  env var when the workflow maps the secret, even if the secret does not exist.

## Keyword Filtering
Keep a posting only if its **description** contains at least one keyword from
`config/keywords.yaml` (case-insensitive **token** match; optional trailing `s`):

`embedded`, `firmware`, `asic`, `fpga`, `rtl`, `mcu`, `microcontroller`,
`micro-controller`, `micro-controllers`, `microcontrollers`

Tune that file, not `parse.py`. Title-only matching misses “Software Engineering
Intern” roles whose FPGA/RTL work is in the body. Do not use raw substring match
(`asic` is a substring of `basic qualifications`).

Skipped intern titles (no keyword hit, or non-US location) go to
`logs/skipped-YYYY-MM-DD.log` (company, title, link — not the description).

**Title noise filter** is separate and runs first: drop non-intern titles so we
never spend HTTP on them. Arm uses a custom `title_keywords` list so `"intern"`
does not match **interconnect**.

**Location filter** (`config/locations.yaml`) runs after the intern title gate
and before keywords. A US country/state/`City, ST` signal **wins** over a
foreign country in the same string (`US and Canada` is kept). Foreign country
*names* plus `foreign_codes` (UK, GBR, … including office suffixes like `UK2`)
drop a listing only when there is no US signal. Do not put 2-letter codes that
collide with US states (`CA`, `IN`, `DE`, `CO`, `ID`) in `foreign_codes`. False
negatives (missed US internships) are worse than a few extra rows — keep
empty/ambiguous locations.

**Log-only buffer:** `first_seen_runs: 0` on current sites, so keyword misses
are dropped immediately. Lookback (below) only applies on the company’s *first*
recorded run. `--dry-run` is the way to preview a write.

## Status Tracking
Each run, intern-titled links still live on that `source_page` are the “open”
set. Sheet rows for that source with status `open` or `applied` whose normalized
link is missing are set to `closed` (row kept). Never move `applied` back to
`open`. Closed-status uses the title-filtered live set, not the keyword-filtered
set — a still-posted intern that fails keywords is not marked closed.

## Reliability & Site Health
- Sequential requests only; split JSON vs HTML delays (see Methods).
- Real User-Agent in `fetch.py` (not the default Python UA).
- `expected_min` on a site: if intern-titled parse count is below that, log a
  warning (possible API/facet break). Many quieter companies use `expected_min: 0`.
- Before adding a company: check `robots.txt` / ToS; prefer a public JSON
  endpoint (Network tab) over CSS selectors; add the entry to `sites.yaml` with
  the right `ats` + board/host fields — do not hardcode companies in `parse.py`.

## Review Workflow
Primary review is the sheet, roughly daily (newest rows at the bottom). `status`
is `open` / `applied` / `closed`. Optional Slack (`SLACK_WEBHOOK_URL`) posts a
short digest of new rows and of per-site failures; it is not required.

## Scheduling
- Cadence: 2 runs/day. Workflow cron: `0 0,12 * * *` UTC (8pm / 8am EST; not
  DST-aware).
- Each run is a **full scan** of all sites (idempotent writes). Runtime does not
  shrink on later runs.
- Prefer GitHub Actions or cron over an always-on process.
- Actions does not read `.env`. Required repo secrets: `GOOGLE_SERVICE_ACCOUNT_JSON`
  (full key file contents) and `GOOGLE_SHEET_ID`. Optional: `GOOGLE_SHEET_WORKSHEET`,
  `SLACK_WEBHOOK_URL`.
- The workflow maps scanner exit `1` (some boards failed) to a warning and a green
  job, because Tesla/Apple/Google often 403 from GitHub IPs and new rows are still
  written. Exit `2` (sheet unavailable) still fails the job.

## Config Conventions (`config/sites.yaml`)
```yaml
- company: Example Corp
  ats: greenhouse          # required; see supported values above
  board: examplecorp       # ATS slug / Workday site / Oracle siteNumber
  url: https://boards.greenhouse.io/examplecorp
  expected_min: 1          # warn if intern-titled count drops below this
  first_seen_runs: 0       # 0 = always enforce keywords (no log-only keep)
  # Workday extras: workday_host, workday_tenant, applied_facets
  # Eightfold extras: domain, eightfold_api (v2|pcsx), query, extra_params
  # Oracle extras: oracle_host, board, query
  # title_keywords: override intern/co-op title gate (see Arm)
```

- Add companies here, not in `parse.py`.
- Prefer intern `applied_facets` / `query` on large Workday/Eightfold/Phenom
  boards so we do not paginate the full catalog. Do not invent Workday facet IDs.
- Conservative title/`query` filters: false negatives (missed internships) are
  worse than a few extra rows to skim — except Arm-style substring traps.

## Error Handling
- One site failing (timeout, 403, layout change) must not crash the run.
- Log `{company}: {exception}` and continue. Exit `1` if any site failed (rows
  from successful sites are still written), `2` if the sheet is unavailable
  (non-dry-run), `0` on a clean run.
- Slack failure digest if `SLACK_WEBHOOK_URL` is set. Repeated-failure tracking
  across runs is not implemented.

## Coding Conventions
- Type-hint everything; `JobPosting` is a dataclass in `src/models.py`.
- No secrets in the repo.
- Keep parsers unit-testable with fake fetchers / fixtures (no network in
  `tests/`). Title-gate and date-parsing belong in those tests.

## Testing
```
python -m pytest
```
Cover link normalization, keyword token match, US location filter, Greenhouse intern-only detail fetches,
TalentBrew card HTML, Amazon-style dates, spreadsheet-ID extraction from a
docs URL, and blank `GOOGLE_SHEET_WORKSHEET` → `Sheet1`.

## First-Run Behavior
When a company has **no** row in `state/company_runs.json`, apply a **3-day**
`date_posted` lookback. Undated postings are treated as “found today” (kept).
Google, Tesla, and TalentBrew often have no dates — lookback will not shrink
those boards. Amazon English dates (`July 29, 2026`) are parsed.

`first_seen_runs` counts successful-or-failed attempts once state is saved (not
on `--dry-run`). With `first_seen_runs: 0` it does not keep keyword misses; it
still matters for the 3-day lookback (lookback is “no row in company_runs”
regardless). A failed first real run still consumes “new company” lookback
on the next real run.

## Open Questions / TODO
- Tune `expected_min` and lookback from skipped/new-row logs after more runs.
- Remaining runtime: 15 Workday sites with no intern facet; Phenom sites with
  empty `query`; Arm TalentBrew full-board list.
- GitHub Actions 30-minute cap vs a slow 35–45 min run.
- `config/urls_skipped.txt` — add only with a known ATS/API.
- Tesla/Apple/Google bot walls from Actions IPs.
- Optional: intern `query` on Phenom; real intern facets on large Workday boards
  once confirmed in the Network tab.
