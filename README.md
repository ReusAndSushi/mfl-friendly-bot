# mfl-friendly-bot

Finds MFL (Metaverse Football League) clubs whose best starting XI has an
overall rating close to yours, then plays friendly matches against them
through the real app.playmfl.com UI.

## How it works

- **Discovery** uses MFL's public, unauthenticated read API
  (`GET /clubs/{id}` and `GET /clubs/{id}/players`) - no login needed for
  this part.
- Club ids are sequential integers with no bulk/rating-filterable listing,
  so a one-time `--build-index` pass sweeps every id into a local
  `club_index.json` cache. Normal runs pre-filter that cache by division
  (a proxy for squad strength) before computing exact ratings, so you're
  not re-scanning thousands of clubs every time.
- **Playing** a friendly is a real, stateful action, so it's done by
  driving an actual browser (Playwright) through the normal "Find A
  Match" flow, using your own logged-in session. First run opens a
  browser window and waits for you to log in manually; after that it
  reuses a saved session file so you don't have to log in every time.
- Your own club's rating is computed by greedily filling a chosen
  formation's slots with your highest-OVR eligible players. An
  opponent's rating is estimated the same way from their public squad,
  since we can't see their private saved tactic.

## Picking a club

You can pass `--club-id 602` directly, or omit it entirely: the script
will open a browser window, detect who you're logged in as (via the
app's own public wallet address, no credentials touched), and print a
numbered list of every club tied to your account for you to pick from.

## Setup

```bash
pip install playwright requests
playwright install chromium

# One-time (takes a few minutes - polite, paced requests over ~11,500 club ids):
python mfl_friendly_bot.py --build-index
```

## Usage

```bash
python mfl_friendly_bot.py --club-id 602 --count 5 --interval 300 --tolerance 3
```

| Flag | Default | Meaning |
|---|---|---|
| `--club-id` | *(none)* | Your club's numeric id. If omitted, pick interactively from your own clubs (see above) |
| `--formation` | `4-3-3` | One of `4-3-3`, `4-4-2`, `4-2-3-1`, `3-5-2`, `3-4-3` |
| `--tolerance` | `3.0` | Max OVR gap to count as "similar" |
| `--division-radius` | `1` | How many divisions above/below yours to consider |
| `--count` | `1` | How many friendlies to play this run |
| `--interval` | `300` | Seconds between friendlies (won't go below MFL's 5-minute cooldown) |
| `--dry-run` | off | Only find/print candidates, don't play anything |

First run of a real play session pops up a Chromium window for a
one-time manual login; the session is cached in `mfl_session.json`
afterward. Re-run `--build-index` occasionally to pick up newly founded
clubs.

## Notes

- `club_index.json` and `mfl_session.json` are generated locally and are
  gitignored - the session file holds your login state and should never
  be committed or shared.
- Opponent ratings are an estimate (best-XI from public squad data), not
  a read of their actual saved tactic, which isn't accessible for clubs
  you don't own.
