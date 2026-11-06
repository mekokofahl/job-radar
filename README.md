# job-radar

[![tests](https://github.com/mekokofahl/job-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/mekokofahl/job-radar/actions)

A personal job-feed monitor. It watches the public job boards of companies you choose, scores every new
opening against rules **you** define (titles, location, pay, travel, industries to avoid), and tells you about the
good ones on Telegram within minutes. A small local web page lets you review, shortlist, and dismiss what it found.

It does **not** apply to anything for you, and it does **not** scrape LinkedIn or other sites that forbid it. It reads
only the public JSON feeds that companies' own career pages already use.

> Built by [Meko Kofahl](https://www.linkedin.com/in/mkofahl/) to run a real job search, in collaboration with
> Claude (Anthropic). I designed the rules, tested it on live data, and ran it daily; Claude wrote most of the code
> as we iterated. See [docs/HOW_THIS_WAS_BUILT.md](docs/HOW_THIS_WAS_BUILT.md) for what that looked like.

## What it does

- **Polls five job-board systems** (six readers, since Lever has US and EU servers): Greenhouse, Lever, Ashby, Workable, and Recruitee.
  Together they host the careers pages of thousands of companies.
- **Scores each opening and explains why.** Every result carries the reasons (`title +7`, `remote_ok +5`, ...) and warnings
  (`US-only remote`, `pay adjusted by location`, `heavy travel: up to ~50%`), so you can audit and tune it.
- **Understands the fine print:** it spots wording that limits who can be hired, location-based pay, travel percentages
  ("30-50% of the time", "one week per month"), and a job board's "remote" flag attached to a single office city.
- **Alerts on Telegram:** immediate alerts above a score threshold, a fast lane for fresh strong matches, a daily digest of
  near-misses, quiet hours, and bot commands to add or remove companies from your phone.
- **Local review page** (`review.py`): mark roles Interested, Applied, or Skip, and copy a shortlist.
- **Diagnostics:** `--stats` (job flow and board health), `--test` (a funnel showing why jobs are filtered out), `--rescore`
  (re-apply changed rules to stored jobs), `--find` (search stored jobs), and a report of which job-board systems a list of
  careers pages uses.

## How it works

```
 companies.csv        config.toml
      |                   |
      v                   v
 +---------+   +-------------------+   +-----------------+   +-------------+
 | ats.py  |-->|    scoring.py     |-->|   jobs.db       |-->|  notify.py  |--> Telegram
 | fetch   |   | title, location,  |   | (SQLite: jobs,  |   +-------------+
 | feeds   |   | pay, travel, ...  |   |  status, flags) |-->|  review.py  |--> local web page
 +---------+   +-------------------+   +-----------------+   +-------------+
      ^                                         ^
      |                                         |
 watchlist.py (add/remove companies, detect a company's job board from its careers page)
```

More detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

Requires Python 3.11 or newer.

```bash
git clone https://github.com/mekokofahl/job-radar.git
cd job-radar
pip install -r requirements.txt

cp config.example.toml config.toml        # Windows: copy config.example.toml config.toml
cp companies.example.csv companies.csv    # Windows: copy companies.example.csv companies.csv
```

1. **Edit `config.toml`** to describe what you want: your home area, titles, salary floors, travel limit. The example file
   models someone based in Germany looking for remote delivery and program roles; change it to fit you. Every setting has a comment.
2. **Try it without Telegram:** `python poller.py --test` fetches every board once and writes a report (`test_report.txt`)
   with a funnel, your top matches, and a score distribution. Use it to tune `alert_min_score`.
3. **Connect Telegram (optional):** create a bot with @BotFather, then run `python telegram_setup.py` and follow the prompts.
4. **Run it:** `python poller.py --loop` checks all boards every few minutes. Leave it running.
5. **Review:** `python review.py` opens a local page at `http://127.0.0.1:8765` (it only listens on your own computer).

On Windows you can use `py` instead of `python`.

## Commands

| Command | What it does |
|---|---|
| `python poller.py --test` | Fetch everything once; write a funnel, top matches, and score distribution (no alerts) |
| `python poller.py --once` / `--loop` | Run one polling cycle, or poll continuously |
| `python poller.py --stats` | Job flow per day, board health, and your best remote-eligible roles (works offline) |
| `python poller.py --rescore` | Re-apply the current rules to every stored job |
| `python poller.py --find "dublin\|remote"` | List stored open jobs whose title or location matches a pattern |
| `python poller.py --test-telegram` / `--digest-now` | Check the Telegram link / send the near-miss digest now |
| `python watchlist.py add "Acme" [careers-url]` | Add a company; its job board is detected automatically |
| `python watchlist.py import file.csv` | Add many companies from a CSV (`name,url,...`) |
| `python watchlist.py unresolved` / `atsreport` | List companies without a readable board; count which board systems a list of careers pages uses |
| `python discover.py names.txt` | Try common board names for a list of company names |
| `python export_csv.py [--interested]` | Export stored jobs (or only your shortlist, with descriptions) to CSV |
| `python review.py` | Local review page: Interested / Applied / Skip |

From Telegram, send your bot `+Company`, `+Company https://careers-page`, `-Company`, `/list`, `/attention`, `/recent`, `/interested`, `/linkedin`, `/help`.

## Configuration in brief

`config.toml` is plain text with a comment on every setting. The main ideas:

- **`title_include`**: patterns with weights. The best match sets a job's base score; no match means the job is ignored.
  **`title_exclude`** drops titles outright.
- **`home_places`, `preferred_relocation`, `onsite_ok_places`**: on-site roles where you live carry no penalty, preferred cities a small one,
  other listed cities a larger one, and everywhere else the largest.
- **`remote_ok` / `remote_eu_ok`**: which location words mean "open to me".
- **`desc_boost`, `avoid_patterns`, `geo_restricted`, `pay_localized`**: phrases in the description that raise or lower a score.
- **`[scores]`, `[travel]`, `[digest]`, `[fast_lane]`, `[company_scores]`**: the numbers that tie it together.

Your `config.toml`, `companies.csv`, and `jobs.db` are listed in `.gitignore` so they never get committed.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

52 tests cover the scoring rules, travel parsing, each job-board reader (against sample feeds), board detection, the Telegram
sender (including that your bot token never appears in an error), and a full run of the pipeline against fake boards with no network.

## Responsible use

- It reads **public** job feeds at a low request rate (a small worker pool, with a tighter limit for hosts that drop busy connections) and retries with backoff.
- It does not log in anywhere, bypass anything, or scrape sites whose terms forbid it. LinkedIn is deliberately not polled; a helper just
  builds search links for you to open yourself.
- It never submits applications. You review and apply.
- Check each job board's terms before you point it at a large list of companies.

## Limitations

- Five job-board systems are supported. Companies on Workday, SmartRecruiters, Personio, Teamtailor, and others are not read yet
  (`watchlist.py atsreport` tells you how many of your companies those are).
- Scoring is rule-based. It is explainable, but it can be wrong; the reasons and flags are there so you can check.
- Remote and location fields differ between job boards, so some classifications will need your judgment.

## License

MIT. See [LICENSE](LICENSE).
