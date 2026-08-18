# Intern Finder

A scheduled scanner for internships in **embedded / firmware / ASIC / FPGA / RTL**. It polls a configured list of company career boards, matches job **descriptions** against keywords, and **appends only new rows** to a Google Sheet.

There is no Playwright and no generic crawler. Each company in `config/sites.yaml` uses a named ATS parser (Greenhouse, Workday, Lever, …). You mark `applied` by hand in the sheet.

## What it does

- Scans ~91 companies listed in `config/sites.yaml` (sequential HTTP, public ATS JSON where possible).
- Title-gates on intern / co-op **before** fetching descriptions.
- Drops postings whose location is clearly non-US (`config/locations.yaml`). Empty / remote / city-only locations are kept.
- Keeps a posting only if the description matches a keyword in `config/keywords.yaml` (token match, so `asic` does not match `basic`).
- Dedupes on the canonical job link. New matches are appended in one write at the end of the run; history is never overwritten.
- Marks previously `open` / `applied` rows `closed` when that link disappears from the company’s live intern-titled set.
- Optional Slack digest of new rows and per-site failures.

It does **not** auto-apply, scrape sites outside `sites.yaml`, or write description text to the sheet.

## Requirements

- Python 3.11+
- A Google Cloud **service account** with access to your spreadsheet
- Optional: a Slack incoming webhook

## Setup

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

Use a native Windows Python (not MSYS2) so `pip` can install wheels. `GOOGLE_SHEET_ID` must be **your** spreadsheet (the value in `.env.example` is only a placeholder).

### Google Sheets

1. Create a Google Cloud service account and download its JSON key as `credentials.json` in the project root (gitignored).
2. Share the spreadsheet with the service account email (`…@….iam.gserviceaccount.com`) as **Editor**.
3. Put the spreadsheet ID (or full docs URL) in `.env`:

```
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
GOOGLE_SHEET_ID=your-spreadsheet-id-or-url
# GOOGLE_SHEET_WORKSHEET=Sheet1
# SLACK_WEBHOOK_URL=
```

You can also paste the JSON into `GOOGLE_SERVICE_ACCOUNT_JSON` instead of using a file.

Sheet columns (created automatically if the first row is empty):

| company | title | link | location | status | date_found | date_posted | source_page |
|---------|-------|------|----------|--------|------------|-------------|-------------|

`link` is the job posting (dedupe key). `source_page` is the career-board URL from `sites.yaml` (used for closed-status). `status` is `open` or `closed` from the scanner; set `applied` yourself. Newest rows are at the bottom. Leave row 1 as headers in A–H only.

## Run

From the project root (do not use an empty `.venv`):

```bash
python -m src.run --dry-run    # fetch + filter, no sheet or state writes
python -m src.run              # write to Google Sheets
python -m pytest
```

`--dry-run` is the way to preview a first run (no sheet or `state/` writes). A company’s **first recorded (non-dry) run** still applies a 3-day `date_posted` lookback; undated postings are kept. Keywords are enforced immediately (`first_seen_runs: 0`).

The sheet does not update until the scan finishes. Typical wall time is **15–25 minutes**. A single site failure is logged and the rest continue. Exit codes: `0` clean, `1` some sites failed, `2` sheet unavailable (non-dry-run).

Logs:

- `logs/run-YYYY-MM-DD.log` — full run (`parsed`, `non-US`, `kept after keywords`, `new`)
- `logs/skipped-YYYY-MM-DD.log` — intern titles dropped by the US-location or keyword filter

## GitHub Actions

[`.github/workflows/intern-finder.yml`](.github/workflows/intern-finder.yml) runs twice a day (`0 0,12 * * *` UTC ≈ 8pm / 8am EST) and on `workflow_dispatch`.

Repo secrets:

| Secret | Required | Purpose |
|--------|----------|---------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | Service-account key JSON |
| `GOOGLE_SHEET_ID` | yes | Spreadsheet ID or URL |
| `GOOGLE_SHEET_WORKSHEET` | no | Worksheet name (default `Sheet1`) |
| `SLACK_WEBHOOK_URL` | no | New-posting / failure digest |

The workflow caches `state/` (`seen_jobs.json`, `company_runs.json`) between runs. Job timeout is 30 minutes; a slow scan (unfaceted Workday catalogs) can hit that cap.

## Config

**Companies** — add entries to `config/sites.yaml`, not to parser code. Supported `ats` values: `greenhouse`, `lever`, `ashby`, `workday`, `eightfold`, `oracle`, `amazon`, `phenom`, `smartrecruiters`, `talentbrew` (alias `smashfly`), `apple`, `google`, `tesla`, `arm`, `html`.

```yaml
- company: Example Corp
  ats: greenhouse
  board: examplecorp
  url: https://boards.greenhouse.io/examplecorp
  expected_min: 1
  first_seen_runs: 0
```

Before adding a company: confirm `robots.txt` / ToS, prefer a public JSON list API, and use intern facets/`query` on large Workday / Eightfold / Phenom boards. Companies still waiting on a parser live in `config/urls_skipped.txt`.

**Keywords** — edit `config/keywords.yaml`. A posting is kept if the description contains any of: `embedded`, `firmware`, `asic`, `fpga`, `rtl`, `mcu`, `microcontroller` (and a few spellings). Matching is case-insensitive **token** match on the **body**, not the title (plurals like `ASICs` still count).

**Locations** — edit `config/locations.yaml`. Drop listings that name a foreign country with no US signal; keep US country/state/`City, ST` forms and ambiguous/empty locations.

## Layout

```
config/sites.yaml      # companies + ATS + board/host/query/facets
config/keywords.yaml   # description-body keywords
config/locations.yaml  # US vs non-US location filter
config/urls.txt        # original career-page inventory
config/urls_skipped.txt
src/run.py             # fetch → parse → filter → dedupe → sheet
src/parse.py           # ATS parsers
state/                 # seen hashes + per-company run counts (gitignored)
logs/
```

Agent-oriented design notes (parsers, lookback, closed-status rules) are in [`AGENTS.md`](AGENTS.md).
