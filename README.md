# Job Jacker

A small job watcher. It checks job boards on a schedule, keeps the postings that
match rules you write in a YAML file, and posts them to a Discord channel through
a webhook. It remembers what it has already sent, so you hear about each job once.

It is one Python process with three dependencies, and it runs the same way on
Windows, Linux, macOS and Docker.

```
job boards -> normalize -> filter -> deduplicate -> Discord
```

## Contents

- [What it does](#what-it-does)
- [Supported sources](#supported-sources)
- [Why Indeed is not supported](#why-indeed-is-not-supported)
- [Installation](#installation)
- [Discord setup](#discord-setup)
- [Configuration](#configuration)
- [How matching works](#how-matching-works)
- [Running it](#running-it)
- [Running with Docker](#running-with-docker)
- [Adding a job source](#adding-a-job-source)
- [Troubleshooting](#troubleshooting)
- [How it behaves toward job boards](#how-it-behaves-toward-job-boards)
- [Project layout](#project-layout)
- [Tests](#tests)

## What it does

Every cycle it:

1. Asks each configured board for current listings.
2. Converts them into one common shape, so nothing downstream cares which board
   a job came from.
3. Applies your filters to the title, company, location, description keywords,
   employment type and salary.
4. Drops anything it has sent before.
5. For the survivors only, optionally reads the posting's own page to pick up the
   description and employment type, then applies the filters again now that there
   is more to go on.
6. Sends what is left to Discord as embeds, and records them.
7. Sleeps until the next cycle.

The order of steps 4 and 5 matters. Reading individual postings is the only
expensive thing here, so it only happens for jobs that are both new and already
look like a match. A typical cycle is a handful of requests.

If a board fails, the failure is logged and the cycle carries on with the others.

## Supported sources

| `board:` | What it covers | Setup needed |
| --- | --- | --- |
| `linkedin` | LinkedIn's public, logged-out job search | None |
| `remotive` | [Remotive](https://remotive.com), remote roles | None |
| `remoteok` | [Remote OK](https://remoteok.com), remote roles | None |
| `rss` | Any board with an RSS or Atom feed | The feed URL |
| `greenhouse` | One company at a time, if it hires through Greenhouse | The company's board name |
| `lever` | One company at a time, if it hires through Lever | The company's handle |
| `adzuna` | Aggregated listings across many boards and employers | A free API key |

Everything except `linkedin` and `rss` uses a documented JSON API. `rss` reads a
feed the site publishes for this purpose. LinkedIn is the only source that parses
HTML, and it is the one most likely to need attention someday; if it breaks, only
`src/sources/linkedin.py` is involved.

Greenhouse and Lever are per-company rather than search-wide. If you have a
shortlist of employers, they are the best sources here, because the listings come
straight from the company's own hiring system and there are no rate limits.

`adzuna` needs a free application id and key from
[developer.adzuna.com](https://developer.adzuna.com). It is the broadest source
available here and covers Canada, the US, the UK and others.

## Why Indeed is not supported

Indeed answers HTTP 403 to any request that does not come from a real browser.
That applies to its search pages and to the RSS endpoint it used to publish.
Monitoring it would mean working around that bot protection, which this project
will not do. Glassdoor and ZipRecruiter are the same story.

Configuring one of them gives you this error instead of a silent failure:

```
'indeed' is not supported. Indeed serves HTTP 403 to any request that is not a
real browser [...] Use the 'adzuna' source for comparable aggregated listings
via an official API.
```

Adzuna is the practical substitute. For specific employers, Greenhouse and Lever
are better than any aggregator.

## Installation

Python 3.10 or newer.

```bash
git clone https://github.com/YOUR_NAME/job-jacker.git
cd job-jacker
python -m venv .venv
```

Activate it, with `.venv\Scripts\activate` on Windows or
`source .venv/bin/activate` elsewhere, then:

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

The three dependencies are `requests`, `PyYAML` and `beautifulsoup4`.

## Discord setup

1. In Discord, open **Server Settings > Integrations > Webhooks**.
2. **New Webhook**, choose the channel it should post in, and give it a name.
3. **Copy Webhook URL**.
4. Put it in `config.yaml` under `discord.webhook_url`, or set the
   `DISCORD_WEBHOOK_URL` environment variable, which takes precedence.

You do not need a bot, a token or any Discord permissions beyond creating the
webhook.

Treat that URL as a password: anyone holding it can post to your channel.
`config.yaml` is in `.gitignore` for that reason, the URL is never written to the
logs, and error messages that might contain it are scrubbed first. On a server,
prefer the environment variable so it never lands in a file at all.

## Configuration

`config.example.yaml` is the complete, commented reference. Copy it and edit.
A short working example:

```yaml
interval_minutes: 60

discord:
  webhook_url: "REPLACE_WITH_YOUR_DISCORD_WEBHOOK_URL"

http:
  user_agent: "job-jacker/1.0 (+https://github.com/YOUR_NAME/job-jacker)"
  delay_seconds: 2

state:
  path: data/state.sqlite3

sources:
  - board: linkedin
    locations:
      - "Ontario, Canada"
    posted_within_days: 7
    fetch_details: true

  - board: remotive
  - board: remoteok

  - board: rss
    feeds:
      - name: We Work Remotely
        url: https://weworkremotely.com/remote-jobs.rss

searches:
  - name: Cybersecurity
    titles:
      include: [SOC Analyst, Security Analyst, Security Engineer]
      exclude: [Senior, Manager, Director]
    keywords:
      include: [SIEM, EDR, SOC, Microsoft Sentinel, incident response]
      exclude: [unpaid, commission only]
    locations:
      include: [Remote, Ontario, Toronto, Canada]
      exclude: [United States]
    employment_types:
      include: [Full-time]
    salary:
      minimum: 70000
      currency: CAD
```

Two notes on the layout.

`sources` says **where** to look. `searches` says **what counts as a match**.
Every source is filtered by every search, so adding a board does not mean
repeating your filters.

`titles.include` does double duty. It is what gets typed into the search box on
the boards that have one (LinkedIn and Adzuna), and it is also used to filter
what comes back. Boards that just hand over their whole list (Remotive, Remote OK,
feeds, Greenhouse, Lever) ignore it for searching and only use it for filtering.

Set `interval_minutes` to at least 5; anything lower is rejected. Hourly is
plenty for a job hunt.

Any string in the file can be `${SOME_VARIABLE}` and it will be read from the
environment, which is how the Adzuna keys stay out of the file.

## How matching works

Each filter group takes `include`, `exclude`, or both:

| Group | Checked against |
| --- | --- |
| `titles` | The job title |
| `companies` | The company name |
| `locations` | The location, plus "Remote" when the board flags a job as remote |
| `keywords` | Title, description and tags together |
| `employment_types` | Full-time, part-time, contract, temporary, internship, volunteer |
| `salary` | `minimum` and `currency`, when the board publishes a figure |

A job has to satisfy every group you configured. Within one group, `include` means
"at least one of these", and `exclude` means "none of these". An `exclude` hit
always wins.

Matching ignores case and punctuation and matches whole words and phrases. So
`SOC` matches "SOC Analyst" but not "Social Media Coordinator", `Microsoft Sentinel`
matches "microsoft-sentinel", and excluding `Lead` does not throw away
"Leadership Development Analyst".

**When a board does not publish a field**, the rule depends on the field. Titles,
companies and locations are always available, so those filters are strict. The
description, employment type and salary are often missing, and a filter on a
missing field is skipped instead of failing. Otherwise a board that publishes no
salary would silently match nothing.

That is why `fetch_details: true` matters on LinkedIn. Its results list has no
descriptions, so without it your `keywords` are only tested against job titles.
With it, each new candidate posting is read once and the keywords apply properly.

Every match also gets a score, shown in the Discord footer: 3 for a title hit,
2 for a company hit, 1 for each description keyword (up to 5), 1 for a location
hit. It is informational unless you set `min_score` on a search, which then
requires that total.

## Running it

```bash
python -m src.main
```

That runs until you stop it with Ctrl+C. Useful flags:

```bash
python -m src.main --test-config   # check the config and print a summary, fetch nothing
python -m src.main --run-once      # one cycle, then exit
python -m src.main --dry-run       # log what would be sent; send nothing, save nothing
python -m src.main --verbose       # debug detail, including why jobs were dropped
python -m src.main --config other.yaml
```

`--dry-run` needs no webhook at all, so it is the fastest way to see whether your
filters do what you meant. Combine it with `--verbose` to see the exact Discord
payload.

A normal cycle looks like this:

```
[12:00:01] Starting job check
[12:00:07] LinkedIn: 24 jobs discovered
[12:00:09] Remotive: 18 jobs discovered
[12:00:12] Remote OK: 100 jobs discovered
[12:00:15] RSS (We Work Remotely): 100 jobs discovered
[12:00:15] 15 of 242 jobs matched your searches
[12:00:15] 11 already sent previously, skipping
[12:00:23] 4 new jobs sent to Discord
[12:00:23] Next check in 60 minutes
```

**The first real run does not post any jobs.** Starting up with an empty state file
would dump every currently open matching job into your channel, so instead it
records them quietly and posts one short "watching" message, which also confirms
your webhook works. From then on you only get new postings. If you would rather see
that first batch, set `notify_on_first_run: true`.

That does not apply to `--dry-run`, which records nothing and so always shows you
its matches.

To run on a schedule instead of continuously, use `--run-once` from cron or Task
Scheduler:

```
0 * * * * cd /opt/job-jacker && .venv/bin/python -m src.main --run-once >> data/cron.log 2>&1
```

Deduplication lives in the state file, not in memory, so restarting or switching
between continuous and scheduled running loses nothing.

## Running with Docker

```bash
cp config.example.yaml config.yaml   # edit it, leave the webhook placeholder alone
echo "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/..." > .env
mkdir -p data
docker compose up -d
docker compose logs -f
```

Create `data` yourself before the first start. The container runs as a non-root
user, and if Docker has to create that directory for the bind mount it will belong
to root, which leaves the container unable to write its state file.

`config.yaml` is mounted read-only and `./data` holds the state file so it survives
rebuilds. Without compose:

```bash
docker build -t job-jacker .
mkdir -p data
docker run -d --name job-jacker \
  -e DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
  -v "$(pwd)/data:/app/data" \
  job-jacker
```

One cycle and out:

```bash
docker compose run --rm job-jacker --config /app/config.yaml --run-once
```

## Adding a job source

Each board lives in its own file under `src/sources/` and returns `Job` objects.
Nothing outside that file knows anything about the board, so this is the only
place you need to touch.

Create `src/sources/example.py`:

```python
from typing import Sequence

from ..models import Job, html_to_text, normalize_employment_type, parse_timestamp
from . import Source


class ExampleSource(Source):
    board = "example"
    options = frozenset({"companies"})       # config keys you accept

    def configure(self) -> None:
        self.companies = self.opt_list("companies")
        if not self.companies:
            self._fail("companies", "is required")

    @property
    def name(self) -> str:
        return f"Example ({', '.join(self.companies)})"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        payload = self.client.get_json("https://example.com/api/jobs")
        return [
            Job(
                board=self.board,
                external_id=str(entry["id"]),
                title=entry["title"],
                company=entry["employer"],
                location=entry.get("city", ""),
                url=entry["url"],
                description=html_to_text(entry.get("body")),
                employment_type=normalize_employment_type(entry.get("type")),
                posted_at=parse_timestamp(entry.get("published")),
            )
            for entry in payload.get("jobs", [])
        ]
```

Then register it in the `BOARDS` dictionary at the bottom of
`src/sources/__init__.py`:

```python
from .example import ExampleSource

BOARDS = {
    ...
    "example": ExampleSource,
}
```

Notes:

- Use `self.client` for every request. It handles timeouts, retries, `Retry-After`
  and the delay between requests. Pass `conditional=True` when you are fetching a
  whole list every time and the server supports ETags.
- Raise nothing on partial failure. Skip entries you cannot read and return what
  you could. Raise `FetchError` only when the whole source failed.
- Set `external_id` to the board's own job id when there is one. Otherwise
  deduplication falls back to the URL, then to company plus title plus location.
- Optionally implement `enrich(job)` to add detail-page data. It is only called
  for jobs that are new and already match, so it stays cheap. See
  `src/sources/linkedin.py`.
- Only `title` and `url` are required. Leave anything else the board does not
  publish alone rather than inventing a value; the matcher knows how to handle a
  missing field.

## Troubleshooting

Start with `--test-config`, then `--dry-run --verbose`. The latter prints one line
per job explaining what happened, including which filter rejected it.

**Nothing arrives in Discord.** If it was the first run, that is intended; look
for the "First run: recording N existing matches" line and check the channel for
the "watching" message. Otherwise check the count lines: "0 jobs discovered" means
the boards gave you nothing, while "0 of 240 jobs matched" means your filters are
too narrow. Loosen one group at a time.

**`Discord rejected the webhook (HTTP 404)`.** The webhook was deleted, or the URL
is wrong or truncated. Create a new one.

**`HTTP 403 (blocked; this source may no longer allow unauthenticated access)`.**
That board is refusing anonymous requests. If it is LinkedIn, increase
`interval_minutes`, reduce `max_queries`, and put a real contact URL in
`http.user_agent`. If it persists, set `enabled: false` on that source; the others
keep working. This project does not try to get around blocks.

**`HTTP 429`.** You are polling too often. The client already honours
`Retry-After`, but raise `interval_minutes` and lower `max_queries` and `pages`.

**`HTTP 404 (not found; check the company or feed name in your config)`.** For
Greenhouse and Lever, the company handle is wrong. Take it from the company's
careers URL: `job-boards.greenhouse.io/THIS_BIT` or `jobs.lever.co/THIS_BIT`.
Companies also move between hiring systems, so a handle that used to work can stop.

**A source reports 0 jobs but no error.** Look for "is unchanged since the last
check". Nothing changed since last time, so it was not downloaded again.

**The same job keeps arriving.** The board is publishing it with a new id or URL
each time. Check `state.retention_days`; you can also confirm what was recorded
with `sqlite3 data/state.sqlite3 "select board, title, company from sent_jobs"`.

**I want to start over.** Stop it and delete the state file. Everything currently
open will be treated as new, so expect the first-run behaviour again.

**`State file ... is unusable; starting a fresh one`.** The file was corrupted,
probably by an abrupt shutdown. It is moved to `.corrupt` and a new one is created.
Nothing is lost except the record of what had been sent.

**Keyword filters seem to be ignored.** The board does not publish descriptions.
Set `fetch_details: true` on LinkedIn, or accept that keywords are matched against
titles only for that source.

## How it behaves toward job boards

A job board should barely notice this running:

- One request at a time. No concurrency.
- A configurable pause between every request, minimum half a second.
- A `User-Agent` that says what it is and where to complain. Put your own URL there.
- ETag and Last-Modified support, so unchanged listings are not downloaded twice.
- `Retry-After` is honoured; 403 and 404 are not retried at all.
- Detail pages are read only for jobs that are new and already match.
- Search terms are capped per cycle (`max_queries`) so a long title list cannot
  quietly turn into hundreds of requests.
- Public, unauthenticated endpoints only. Nothing here logs in, stores cookies or
  works around bot protection. A board that cannot be read without doing those
  things is listed as unsupported instead.

Remote OK's API terms ask that you link back to the posting on Remote OK and name
it as the source. The notifications do both.

## Project layout

```
src/
  main.py          CLI, the cycle, and the wait between cycles
  config.py        loading and validating config.yaml
  models.py        the Job record and the normalizing helpers
  matching.py      the filters and the score
  state.py         SQLite: what has been sent, plus HTTP cache validators
  notify.py        building and sending Discord embeds
  http_client.py   the shared HTTP session, with the delays and retries
  sources/
    __init__.py    the Source base class and the board registry
    linkedin.py    the only source that parses HTML
    greenhouse.py  lever.py  remotive.py  remoteok.py  rss.py  adzuna.py
tests/
config.example.yaml
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

They use only the standard library. They cover config validation, every filter
rule, deduplication and restart, each source's parsing against real response
shapes, malformed and truncated input, Discord payload construction and
redaction, and a full cycle with a broken source in it.

## License

MIT. See [LICENSE](LICENSE).
