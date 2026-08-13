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
- [Turning boards on and off](#turning-boards-on-and-off)
- [How matching works](#how-matching-works)
- [Running it](#running-it)
- [Running with Docker](#running-with-docker)
- [Everyday commands](#everyday-commands)
- [Clearing what it remembers](#clearing-what-it-remembers)
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

Indeed is not on that list, and neither are Glassdoor and ZipRecruiter.
[Here is why](#why-indeed-is-not-supported). Use `adzuna` for the same kind of
broad coverage.

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
git clone https://github.com/alloct/job-jacker.git
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
  user_agent: "job-jacker/1.0 (+https://github.com/alloct/job-jacker)"
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

## Turning boards on and off

**To stop using a board for good**, delete its `- board:` entry from `sources`.

**To keep the settings but skip the board**, add `enabled: false` to the entry.
That is how `config.example.yaml` ships Greenhouse, Lever and Adzuna: the entries
are there with working examples, ignored until you flip them on.

```yaml
sources:
  - board: linkedin           # active
    locations:
      - "Ontario, Canada"

  - board: adzuna             # configured, but skipped
    enabled: false
    country: ca
    app_id: "${ADZUNA_APP_ID}"
    app_key: "${ADZUNA_APP_KEY}"
```

A disabled entry is not validated, so it is fine to leave a half-finished one in
the file. Missing settings and unset environment variables inside it will not stop
startup. Remove the `enabled: false` line, or set it to `true`, when you are ready.

**For a single run**, `--only` narrows things down without editing the file:

```bash
python -m src.main --only remotive --run-once
```

It takes the `board:` name rather than the display name, so `--only rss` covers
every feed you have configured. It can only choose among the boards that are
enabled; it will not wake a disabled one up.

After any change, `--test-config` lists exactly what will be used:

```
  Sources (2):
      - LinkedIn
      - Remotive
```

At least one board has to be enabled; turning them all off is rejected with
`every source is disabled; enable at least one`. Turning a board off does not lose
its history, so switching it back on will not resend jobs you have already had,
unless they have aged past `state.retention_days` in the meantime.

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

That runs until you stop it with Ctrl+C. The flags:

| Flag | What it does |
| --- | --- |
| `--test-config` | Validate the config, print a summary, fetch nothing |
| `--test-webhook` | Post one sample job to Discord, then exit |
| `--run-once` | One cycle, then exit |
| `--dry-run` | Log what would be sent; send nothing, save nothing |
| `--only BOARD` | Check just that board. Repeatable |
| `--forget-jobs` | Clear the record of jobs already sent, then exit |
| `--clear-http-cache` | Clear the stored ETags, then exit |
| `--verbose` | Debug detail, including why each job was kept or dropped |
| `--config PATH` | Use a config file other than `./config.yaml` |

They combine. `--dry-run` needs no webhook at all, so `--dry-run --run-once
--verbose` is the fastest way to see whether your filters do what you meant, and
it prints the exact Discord payload without touching the state file.

A normal cycle looks like this:

```
[12:00:01] Starting job check
[12:00:07] LinkedIn: 24 jobs discovered
[12:00:09] Remotive: 18 jobs discovered
[12:00:12] Remote OK: 100 jobs discovered
[12:00:15] RSS (We Work Remotely): 100 jobs discovered
[12:00:15] 15 of 242 jobs matched your searches
[12:00:15] 9 already sent previously, skipping
[12:00:22] 2 jobs stopped matching once the full posting was read: no keyword match
[12:00:23] 4 new jobs sent to Discord
[12:00:23] Next check in 60 minutes
```

Every line that reduces the count says why it did, so a cycle that sends nothing
still tells you what happened to each job.

**The first real run does not post any jobs.** Starting up with an empty state file
would dump every currently open matching job into your channel, so instead it
records them quietly and posts one short "watching" message, which also confirms
your webhook works. From then on you only get new postings. If you would rather see
that first batch, set `notify_on_first_run: true`.

That does not apply to `--dry-run`, which records nothing and so always shows you
its matches.

To run on a schedule instead of continuously, use `--run-once` from cron:

```
0 * * * * cd /opt/job-jacker && .venv/bin/python -m src.main --run-once >> data/cron.log 2>&1
```

or hourly from Windows Task Scheduler:

```
schtasks /create /tn "Job Jacker" /sc hourly /tr "cmd /c cd /d C:\job-jacker && .venv\Scripts\python.exe -m src.main --run-once >> data\task.log 2>&1"
```

Both need the `cd`: the config path and the state path are relative to the working
directory.

To keep the continuous process alive across reboots and crashes on Linux, without
Docker, `/etc/systemd/system/job-jacker.service`:

```ini
[Unit]
Description=Job Jacker
After=network-online.target

[Service]
User=jobjacker
WorkingDirectory=/opt/job-jacker
Environment=DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
ExecStart=/opt/job-jacker/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now job-jacker
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
docker compose run --rm job-jacker --run-once
```

Every flag listed under [Running it](#running-it) works the same way there.
`docker compose run` replaces the container's default arguments, and the config is
still found at `/app/config.yaml`.

## Everyday commands

The local commands assume you are in the project directory with the virtualenv
active. The Docker forms do the same thing.

**Send a test notification.** Posts one made-up job to your channel and exits.
Do this after creating the webhook, after moving to another machine, and any time
notifications stop arriving.

```bash
python -m src.main --test-webhook
docker compose run --rm job-jacker --test-webhook
```

**Check a config change before it goes live.** Neither of these contacts a job
board or Discord.

```bash
python -m src.main --test-config
python -m src.main --dry-run --run-once --verbose
```

**Run a check right now**, outside the schedule, sending and recording as usual:

```bash
python -m src.main --run-once
docker compose run --rm job-jacker --run-once
```

An ad-hoc run shares the state file with any instance already running, so it will
not resend jobs that have been announced before.

**Check one board** when you are chasing a problem with it, leaving the others
out of the output:

```bash
python -m src.main --only linkedin --dry-run --run-once --verbose
```

**Read the logs.** Everything goes to the console. Uncomment `log.file` in
`config.yaml` to also write to a file, which is what you want under systemd or
Task Scheduler.

```bash
docker compose logs -f --tail 100         # Docker
journalctl -u job-jacker -f               # systemd
tail -f data/job-jacker.log               # log.file is set
Get-Content data\job-jacker.log -Wait     # the same on Windows
```

**Stop, start and restart.** Locally it is Ctrl+C and run it again; nothing is
lost either way.

```bash
docker compose restart                    # Docker
docker compose down                       # stop and remove the container
sudo systemctl restart job-jacker         # systemd
```

**Update to a newer version.** Config and state carry over untouched.

```bash
git pull
pip install -r requirements.txt           # only matters if the dependencies changed
python -m src.main --test-config          # confirm your config still validates
docker compose up -d --build              # Docker: rebuild and restart in one step
```

Compare your `config.yaml` against `config.example.yaml` after an update to see
whether new options appeared. Unknown keys are rejected rather than ignored, so a
config that validates is a config the new version fully understands.

## Clearing what it remembers

The state file holds two separate things, and they are cleared separately.

| What | Why you would clear it | Effect |
| --- | --- | --- |
| Sent jobs | You want postings you have already been shown to come through again | Everything currently open counts as new |
| HTTP cache | A board keeps reporting "unchanged since the last check" and you want a full download | The next cycle downloads every list in full |

Neither command needs a webhook or network access, and neither is affected by a
board being unreachable.

**Clear the record of sent jobs.** Everything open at the boards you watch becomes
new again:

```bash
python -m src.main --forget-jobs
docker compose run --rm job-jacker --forget-jobs
```

It reports what it removed, and tells you plainly when there was nothing there:

```
[15:26:55] Forgot 2 sent job(s). The next run starts from scratch, which means the first-run rules apply again.
[15:27:10] No sent jobs were on record, so there was nothing to forget
```

That second line is normal on a setup that has not sent anything yet. `--test-config`
prints the same figure as `Jobs on record`, and it appears in the startup line of
every run.

Read that last part before you use it. An empty record means the next run is a
first run, so by default it quietly re-records everything instead of posting it,
and you are back where you started with nothing announced. To actually receive
those jobs again, set `notify_on_first_run: true` before the next cycle.

**Clear the HTTP cache**, which is the ETags and Last-Modified values that let
boards answer "nothing changed" instead of resending a list:

```bash
python -m src.main --clear-http-cache
docker compose run --rm job-jacker --clear-http-cache
```

This never causes duplicate notifications. Redownloading a list you already have
just means every job in it is recognised and skipped. It also happens on its own
after any cycle that failed to deliver something, so a cached "nothing changed"
cannot hide a job you never received.

**Clear both, or start from an empty file.** Combine the flags, or stop it and
delete the file:

```bash
python -m src.main --forget-jobs --clear-http-cache
rm data/state.sqlite3*                    # del data\state.sqlite3* on Windows
```

Deleting the file does the same thing as both flags together. Use the flags while
something is running, and the delete when you would rather not think about it. The
`*` matters: SQLite keeps `-wal` and `-shm` companions next to the database.

**Forget one job instead of all of them.** For this you need the `sqlite3`
command-line tool, which is a separate download from the Python module of the same
name. Look before you delete:

```bash
sqlite3 data/state.sqlite3 "select sent_at, board, company, title from sent_jobs order by sent_at desc limit 20;"
sqlite3 data/state.sqlite3 "delete from sent_jobs where title like '%Security Analyst%';"
sqlite3 data/state.sqlite3 "delete from sent_jobs where board = 'linkedin';"
```

Deleting a single row does not trigger the first-run behaviour, so those jobs are
simply announced again on the next cycle. This is the one case that needs SQL;
everything else has a flag.

Sent jobs also expire on their own after `state.retention_days`, 90 by default. A
posting that has been down that long can be announced again if it comes back.

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

Three commands answer most questions, in this order:

```bash
python -m src.main --test-config                  # is the config valid
python -m src.main --test-webhook                 # can it reach Discord
python -m src.main --dry-run --run-once --verbose # what did the boards return, and why was each job kept or dropped
```

**Nothing arrives in Discord.** Run `--test-webhook`. If the sample job appears,
the delivery path is fine and the problem is upstream of it. If it was the first
run, that is intended; look for the "First run: recording N existing matches" line
and check the channel for the "watching" message. Otherwise read the count lines:
"0 jobs discovered" means the boards gave you nothing, while "0 of 240 jobs matched"
means your filters are too narrow, and the "Why nothing matched" line that follows
it names the filters responsible.

**`Discord rejected the webhook (HTTP 404)`.** The webhook was deleted, or the URL
is wrong or truncated. Create a new one.

**`HTTP 403 (blocked; this source may no longer allow unauthenticated access)`.**
That board is refusing anonymous requests. If it is LinkedIn, increase
`interval_minutes`, reduce `max_queries`, and put a real contact URL in
`http.user_agent`. If it persists,
[turn that source off](#turning-boards-on-and-off); the others keep working. This
project does not try to get around blocks.

**`HTTP 429`.** You are polling too often. The client already honours
`Retry-After`, but raise `interval_minutes` and lower `max_queries` and `pages`.

**`HTTP 404 (not found; check the company or feed name in your config)`.** For
Greenhouse and Lever, the company handle is wrong. Take it from the company's
careers URL: `job-boards.greenhouse.io/THIS_BIT` or `jobs.lever.co/THIS_BIT`.
Companies also move between hiring systems, so a handle that used to work can stop.
Check a corrected one on its own with `--only greenhouse --dry-run --run-once`.

**A source reports 0 jobs but no error.** Look for "is unchanged since the last
check". Nothing changed since last time, so it was not downloaded again. If you
believe that is wrong, `--clear-http-cache` forces a full download next cycle.

**Too few jobs match.** Every step that discards a job says why, so work down the
log. `--verbose` adds one line per rejected job, which is the quickest way to see
whether one filter is doing all the damage:

```
[17:21:59] DEBUG: Senior Security Analyst at Example did not match: excluded title: Senior
[17:21:59] DEBUG: Analyst, Cyber Security at Dream did not match: no title match
[17:21:59] 9 of 22 jobs matched your searches
```

`no title match` means `titles.include` needs another phrasing; boards title the
same role a dozen ways, and matching is on whole words, so "Security Analyst" does
not catch "Analyst, Cyber Security". `excluded title: Senior` is `titles.exclude`
working as asked, which is worth checking if you are excluding a common word.

**Jobs matched, but none were sent.** Two different things produce this, and the log
distinguishes them. "N already sent previously, skipping" means you have had them
before. "N jobs stopped matching once the full posting was read" means they passed
on title, company and location, then failed once `fetch_details` fetched the
description:

```
[21:02:15] 1 job stopped matching once the full posting was read: no keyword match
```

That is `keywords.include` doing its job, and it is the usual reason a search with a
long keyword list sends almost nothing: the posting has to contain at least one of
your keywords. Shorten the list to the terms you actually insist on, or drop
`keywords.include` and let titles do the filtering. Setting `fetch_details: false`
also stops keywords being tested against descriptions, since a board that publishes
no description has its keyword filter skipped.

**`--forget-jobs` says it forgot 0.** The record was already empty, which is normal
if no cycle has ever sent anything. `--test-config` shows the same number as `Jobs
on record`. Note that ETags are stored separately, so a board can still say
"unchanged since the last check" while no jobs are recorded at all.

**The same job keeps arriving.** The board is publishing it with a new id or URL
each time, so it looks like a different posting. Check `state.retention_days`, and
confirm what was actually recorded with the query under
[Clearing what it remembers](#clearing-what-it-remembers).

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
