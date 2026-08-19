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

Use a native Windows Python (not MSYS2) so `pip` can install wheels. `.env` and `credentials.json` are gitignored; GitHub Actions never reads them — use **repo secrets** for CI (below).

### Google Sheets

This is a **service account**, not OAuth as you. Share the spreadsheet with that robot email, then put its JSON key and the sheet ID in `.env` (local) or GitHub secrets (Actions).

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **Google Sheets API** and **Google Drive API** (Drive is required so `gspread` can open the file by ID).
2. **IAM & Admin → Service accounts → Create service account.** Skip a GCP IAM role; access comes from sharing the sheet. **Keys → Add key → JSON**, save as `credentials.json` in the project root.
3. Open that JSON and copy `client_email` (`…@….iam.gserviceaccount.com`). In Google Sheets, **Share** that address as **Editor**. Uncheck “Notify people” (it is not a real inbox).
4. Copy `.env.example` to `.env` and set the spreadsheet ID (the token after `/d/` in the docs URL, or the full URL):

```
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
GOOGLE_SHEET_ID=your-spreadsheet-id-or-url
# GOOGLE_SHEET_WORKSHEET=Sheet1
# SLACK_WEBHOOK_URL=
```

Leave the tab named `Sheet1`, or set `GOOGLE_SHEET_WORKSHEET` to the exact tab name. A blank value also falls back to `Sheet1`. If `GOOGLE_SERVICE_ACCOUNT_JSON` is set (the full key JSON as one string), it is used instead of the file.

Sheet columns (written automatically if row 1 is empty):

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

The sheet does not update until the scan finishes. Typical wall time is **15–25 minutes**. A single site failure is logged and the rest continue. Exit codes: `0` clean, `1` some sites failed (rows still written), `2` sheet unavailable (non-dry-run).

Logs:

- `logs/run-YYYY-MM-DD.log` — full run (`parsed`, `non-US`, `kept after keywords`, `new`)
- `logs/skipped-YYYY-MM-DD.log` — intern titles dropped by the US-location or keyword filter

## GitHub Actions

[`.github/workflows/intern-finder.yml`](.github/workflows/intern-finder.yml) runs twice a day (`0 0,12 * * *` UTC ≈ 8pm / 8am EST) and on `workflow_dispatch`.

The workflow does **not** load `.env` or `credentials.json` from the repo (both are gitignored). GitHub does not infer secret meaning from names: you create secrets with **these exact names**, the workflow copies them into env vars of the same name, and `src/sheet.py` / `src/notify.py` read them with `os.getenv`.

**Settings → Secrets and variables → Actions → New repository secret.** Add:

| Secret | Required | Value to paste |
|--------|----------|----------------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | Entire contents of `credentials.json` (one JSON object, including `private_key` and `client_email`) |
| `GOOGLE_SHEET_ID` | yes | Same spreadsheet ID or docs URL as local `.env` |
| `GOOGLE_SHEET_WORKSHEET` | no | Tab name at the bottom of the spreadsheet. Omit or leave blank to use `Sheet1` |
| `SLACK_WEBHOOK_URL` | no | Incoming webhook URL for new-posting / failure digests |

The JSON secret is written to `credentials.json` on the runner; the scan step then sets `GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json`. Same service account, same shared spreadsheet as local runs.

If some career boards 403 (Tesla / Apple / Google often do from GitHub IPs), the scanner exits `1` but still writes any new rows. The workflow treats that as a **warning** and keeps the job green. Exit `2` (sheet unavailable / missing secrets) still fails the job.

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

**Locations** — edit `config/locations.yaml`. Drop listings that name a foreign country with no US signal; a US country/state/`City, ST` match wins (`US and Canada` is kept). Keep ambiguous/empty locations.

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
