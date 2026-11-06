# Architecture

job-radar is a small set of Python modules around a SQLite database. There is no framework and the only dependencies are
`requests` and `tzdata`.

| Module | Responsibility |
|---|---|
| `ats.py` | One reader per job-board system. Each turns a company's public JSON feed into the same job shape: `id, title, location, workplace, remote, url, posted_at, description`. Includes retries with backoff and a per-host concurrency limit. |
| `scoring.py` | `Scorer.evaluate(job, company, meta)` returns a score, a location class, human-readable `reasons`, and `flags`, or `None` with a `reject` reason. Pure functions over text and config; no network, so it is easy to test. |
| `poller.py` | Orchestration: load config, fetch all companies in parallel, score new jobs, store them, send alerts, and run the diagnostic commands (`--test`, `--stats`, `--find`, `--rescore`). |
| `watchlist.py` | Add and remove companies; detect which job-board system a careers page uses (reads the page and, if needed, its JavaScript files); Telegram command listener. |
| `notify.py` | Telegram sending: splits long messages under the 4,096-character limit and never lets the bot token appear in errors. |
| `review.py` | A tiny local web server (127.0.0.1 only) with a per-session token for the buttons that change a job's status. |
| `linkedin_links.py` | Builds LinkedIn search links ("posted in the past hour") for you to open. It makes no requests to LinkedIn. |
| `discover.py`, `export_csv.py`, `telegram_setup.py` | Helpers. |

## Data flow

1. `watchlist.read_companies()` gives the companies to check (`companies.csv`).
2. `poller.fetch_all()` calls each company's reader in a thread pool.
3. For each job not already in `jobs.db`, `Scorer.evaluate()` decides whether it is relevant and how well it fits.
4. New relevant jobs are stored with their score, reasons, and flags. Jobs above the alert threshold (and strong fresh ones in the
   "fast lane") are sent to Telegram; near-misses wait for the daily digest.
5. Every cycle also refreshes `last_seen`, so postings that disappear are marked inactive.

## Scoring in one paragraph

A title match gives the base score. Then adjustments: location class (remote and open to you, remote but restricted, on-site in your home
area, on-site in a place you would consider, on-site elsewhere), description boosts (AI, executives, enterprise customers...), company
metadata (stage, recent funding, size), salary against your floors, travel against your limit, and penalties for wording that limits who can
be hired or says pay depends on location. All numbers live in `config.toml`.

## Design decisions

- **Explainability over cleverness.** Rules and weights are visible in one file, and every job carries its reasons.
- **Fail loudly in diagnostics, quietly in alerts.** `--test` shows exactly why jobs were filtered; alerts stay short.
- **Scores are computed once, when a job is first seen,** so a rule change does not alter history. `--rescore` re-applies rules on demand.
- **Public feeds only.** No login, no scraping of sites that prohibit it.
- **Secrets stay out of logs.** The Telegram sender strips the token from every exception.
