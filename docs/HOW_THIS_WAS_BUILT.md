# How this was built

job-radar started as a practical need: I was searching for remote roles and wanted to hear about good openings quickly,
without refreshing a dozen career pages. I built it with Claude (Anthropic's AI assistant) in a long working conversation.

## Who did what

- **I decided what it should do and what it should not do:** the scoring criteria, the filters, the alert behavior, and the boundaries
  (no auto-apply, no scraping of sites that forbid it).
- **Claude wrote most of the code,** proposed designs, and explained trade-offs, usually in small increments I could run and check.
- **I ran it on real data every day,** read the output critically, and reported what looked wrong. Most of the improvements came
  from that loop, not from the first draft.

## Problems found in real use, and fixed

| What I saw | Cause | Fix |
|---|---|---|
| A role scored high that I could never take | "United States" in a location counted as "remote anywhere in the US", and many US-only postings do not hire elsewhere | Wording checks for country restrictions, a smaller US-only score, and an explicit warning |
| Telegram rejected a notice with an error | A bulk import produced a message over Telegram's 4,096-character limit | Messages are split and capped; the error also exposed the bot token, so errors are now scrubbed |
| A high score for an on-site New York role | The on-site penalty was too small; investigating also showed that a board's "remote" flag attached to a single office city could be read as remote | A larger on-site penalty, and a single city with only a board flag is now treated as on-site and flagged for review |
| A rescore silently dropped a legitimate "Embedded Finance" role | An exclusion rule meant for embedded-software jobs was too broad | The rule was narrowed and a regression test added |
| "No new jobs" for days | Most openings on the watchlist were on-site or restricted to other regions | Added `--test` funnel diagnostics so filtering, supply, and coverage can be told apart |
| Many companies' job boards were not detected | Career pages that load their job list with JavaScript hide the board | Board detection now also reads a page's script files |

## Verification

The scoring rules, each job-board reader, board detection, the Telegram sender, and a full run of the pipeline against fake boards are covered
by automated tests (`python -m unittest discover -s tests -t .`). When I generalized the code from my own settings into a configurable
tool, I checked that results for my own configuration were identical before and after across more than a thousand test combinations.

## What I would do next

Readers for Workday, SmartRecruiters, and Personio (guided by which boards my own list actually uses), better handling of seniority levels,
and a way to feed outcomes (applied, interviewed, rejected) back into the weights.
