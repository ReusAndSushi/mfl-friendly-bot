"""
MFL Friendly Bot
=================

Finds opponent clubs on Metaverse Football League (app.playmfl.com) whose
best starting XI (for a given formation) has an overall rating close to
your own club's, then plays friendly matches against them through the real
MFL web UI (using your own logged-in browser session via Playwright).

Design notes
------------
- Discovery uses MFL's public, unauthenticated read API:
    GET /clubs/{id}          -> division, status, friendlyPref, name
    GET /clubs/{id}/players  -> each player's positions + overall rating
  No login or token is needed or touched for this part. (The app's
  in-app "search a club by name" endpoint turned out to require auth, so
  this script does NOT use it - see build_index() below.)
- There is no bulk/rating-filterable club listing, and club ids are just
  sequential integers (roughly 1..11500 as of testing). So discovery is
  two phases:
    1) `--build-index`: a one-time (or occasional refresh) sweep of every
       club id's lightweight /clubs/{id} record, cached to club_index.json.
       This is the only part that touches a large chunk of the API, and
       it only fetches small club records, not full squads.
    2) Normal runs: load the cached index, pre-filter to clubs in a nearby
       *division* (division is a reasonable proxy for a club's ballpark
       squad strength) that have friendlies enabled and aren't excluded,
       THEN fetch full player lists (the heavier call) only for that
       shortlist to compute an exact rating.
- Playing a friendly is a real, stateful action, so it's done by driving an
  actual browser through the normal "Find A Match" UI flow, using YOUR
  session. The first time you run this script a browser window opens and
  waits for you to log in manually; after that it reuses a saved session
  file (mfl_session.json) so you don't have to log in every time.
- MFL enforces a cooldown between friendlies (observed: 5 minutes, and
  clubs also carry their own friendlyPrefCooldown timestamp). This script
  will not let --interval go below the observed minimum, and skips clubs
  whose friendlyPref is DISABLED or whose cooldown hasn't elapsed.
- Opponent "best XI" is an ESTIMATE: since we can't see another club's
  private saved tactic, we approximate it by greedily filling a formation's
  slots with their highest-OVR players who list that position, from their
  public squad list. Your own club's rating is computed the same way for
  a fair comparison.

Usage
-----
    pip install playwright requests
    playwright install chromium

    # One-time (takes a while - politely paced requests over ~11,500 ids):
    python mfl_friendly_bot.py --build-index

    # Normal use:
    python mfl_friendly_bot.py --club-id 602 --count 5 --interval 300 --tolerance 3

First run of a play session pops up a browser window and pauses for you to
log into MFL. Close nothing manually - just log in, then return to the
terminal and press Enter when prompted.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

API = "https://z519wdyajg.execute-api.us-east-1.amazonaws.com/prod"
SESSION_FILE = Path(__file__).parent / "mfl_session.json"
INDEX_FILE = Path(__file__).parent / "club_index.json"
MIN_INTERVAL_SECONDS = 300  # observed 5-minute cooldown per friendly
MAX_CLUB_ID_GUESS = 12000   # generous upper bound; real max detected at build time
INDEX_WORKERS = 8           # concurrent requests while building the index - keep polite

# Slot order matters: we fill defense first, then midfield, then attack.
FORMATIONS = {
    "4-3-3": ["GK", "LB", "CB", "CB", "RB", "CDM", "CM", "CM", "LW", "ST", "RW"],
    "4-4-2": ["GK", "LB", "CB", "CB", "RB", "LM", "CM", "CM", "RM", "ST", "ST"],
    "4-2-3-1": ["GK", "LB", "CB", "CB", "RB", "CDM", "CDM", "CAM", "LW", "RW", "ST"],
    "3-5-2": ["GK", "CB", "CB", "CB", "LM", "CM", "CDM", "CM", "RM", "ST", "ST"],
    "3-4-3": ["GK", "CB", "CB", "CB", "LM", "CM", "CM", "RM", "LW", "ST", "RW"],
}


def fetch_json(url, session=None, retries=3):
    for attempt in range(retries):
        r = (session or requests).get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
    r.raise_for_status()


def get_club(club_id, session=None):
    return fetch_json(f"{API}/clubs/{club_id}", session=session)


def get_players(club_id, session=None):
    return fetch_json(f"{API}/clubs/{club_id}/players", session=session) or []


def best_xi_rating(players, formation):
    """Greedily fill formation slots with highest-OVR eligible players.
    Falls back to best remaining player if no positional match is left.
    Returns (average_ovr, list_of_(slot, player_name, ovr))."""
    slots = FORMATIONS[formation]
    pool = sorted(players, key=lambda p: p["metadata"]["overall"], reverse=True)
    used_ids = set()
    picks = []

    for slot in slots:
        candidate = next(
            (p for p in pool
             if p["id"] not in used_ids and slot in p["metadata"].get("positions", [])),
            None,
        )
        if candidate is None:
            candidate = next((p for p in pool if p["id"] not in used_ids), None)
        if candidate is None:
            break
        used_ids.add(candidate["id"])
        name = f"{candidate['metadata'].get('firstName', '')} {candidate['metadata'].get('lastName', '')}".strip()
        picks.append((slot, name, candidate["metadata"]["overall"]))

    if not picks:
        return 0, []
    avg = sum(ovr for _, _, ovr in picks) / len(picks)
    return avg, picks


def squad_of(club_id, session=None):
    """Players currently under an active contract with this club."""
    players = get_players(club_id, session=session)
    return [p for p in players if p.get("activeContract", {}).get("club", {}).get("id") == club_id]


# ---------------------------------------------------------------------------
# Index building (one-time / occasional refresh)
# ---------------------------------------------------------------------------

def detect_max_club_id(session):
    lo, hi = 1, MAX_CLUB_ID_GUESS
    while lo < hi:
        mid = (lo + hi + 1) // 2
        club = get_club(mid, session=session)
        if club is not None:
            lo = mid
        else:
            hi = mid - 1
    return lo


def build_index():
    session = requests.Session()
    print("Detecting highest club id in use...")
    max_id = detect_max_club_id(session)
    print(f"Scanning club ids 1..{max_id} ({max_id} requests, {INDEX_WORKERS} at a time)...")

    records = {}
    done = 0

    def fetch_one(cid):
        try:
            return cid, get_club(cid, session=session)
        except requests.RequestException:
            return cid, None

    with ThreadPoolExecutor(max_workers=INDEX_WORKERS) as ex:
        futures = [ex.submit(fetch_one, cid) for cid in range(1, max_id + 1)]
        for fut in as_completed(futures):
            cid, club = fut.result()
            done += 1
            if club and club.get("status") == "FOUNDED":
                records[str(cid)] = {
                    "id": cid,
                    "name": club.get("name"),
                    "division": club.get("division"),
                    "friendlyPref": club.get("friendlyPref"),
                    "friendlyPrefCooldown": club.get("friendlyPrefCooldown", 0),
                }
            if done % 500 == 0:
                print(f"  ...{done}/{max_id}")

    INDEX_FILE.write_text(json.dumps(records))
    print(f"\nSaved {len(records)} founded clubs to {INDEX_FILE.name}")


def load_index():
    if not INDEX_FILE.exists():
        print("No club_index.json found. Run with --build-index first (one-time, takes a while).")
        sys.exit(1)
    return json.loads(INDEX_FILE.read_text())


# ---------------------------------------------------------------------------
# Candidate discovery using the cached index
# ---------------------------------------------------------------------------

def find_similar_opponents(my_club_id, formation, tolerance, division_radius, max_lookups=500):
    session = requests.Session()
    my_squad = squad_of(my_club_id, session=session)
    my_rating, my_picks = best_xi_rating(my_squad, formation)
    print(f"Your club (#{my_club_id}) best-XI ({formation}) rating: {my_rating:.1f}")
    for slot, name, ovr in my_picks:
        print(f"    {slot:4s} {name:25s} {ovr}")

    my_club = get_club(my_club_id, session=session)
    my_division = my_club.get("division")

    index = load_index()
    now_ms = time.time() * 1000

    shortlist = [
        rec for rec in index.values()
        if rec["id"] != my_club_id
        and rec.get("friendlyPref") != "DISABLED"
        and rec.get("friendlyPrefCooldown", 0) <= now_ms
        and my_division is not None
        and rec.get("division") is not None
        and abs(rec["division"] - my_division) <= division_radius
    ]
    shortlist = shortlist[:max_lookups]
    print(f"\n{len(shortlist)} candidates in division {my_division}±{division_radius} "
          f"with friendlies enabled - computing ratings...")

    matches = []

    def rate_one(rec):
        try:
            squad = squad_of(rec["id"], session=session)
        except requests.RequestException:
            return None
        if not squad:
            return None
        rating, _ = best_xi_rating(squad, formation)
        gap = abs(rating - my_rating)
        if gap <= tolerance:
            return {"id": rec["id"], "name": rec["name"], "rating": rating, "gap": gap}
        return None

    with ThreadPoolExecutor(max_workers=INDEX_WORKERS) as ex:
        for result in ex.map(rate_one, shortlist):
            if result:
                matches.append(result)

    matches.sort(key=lambda m: m["gap"])
    return my_rating, matches


# ---------------------------------------------------------------------------
# Actually playing friendlies (real browser, real session)
# ---------------------------------------------------------------------------

def play_friendlies(my_club_id, matches, count, interval):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        if SESSION_FILE.exists():
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(storage_state=str(SESSION_FILE))
            page = context.new_page()
        else:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://app.playmfl.com")
            print("\nA browser window has opened. Please log into MFL there.")
            input("Once you're logged in and see your dashboard, press Enter here to continue...")
            context.storage_state(path=str(SESSION_FILE))

        played = 0
        for m in matches:
            if played >= count:
                break
            print(f"\n[{played + 1}/{count}] Challenging {m['name']} (rating {m['rating']:.1f}, "
                  f"gap {m['gap']:.1f})")
            try:
                page.goto(f"https://app.playmfl.com/clubs/{my_club_id}")
                page.get_by_role("button", name="Find A Match").click()
                search_box = page.get_by_placeholder("Team Name, Owner Name, Owner's Discord")
                search_box.fill(m["name"])
                page.wait_for_timeout(1500)
                # Each result row is a div.flex.flex-row.items-center.gap-4.py-4
                # containing the club name text and a "Play" button (verified
                # against the live DOM). Filter rows by the candidate's exact name.
                row = page.locator("div.flex.flex-row.items-center.gap-4.py-4").filter(
                    has_text=m["name"]
                ).first
                row.wait_for(timeout=5000)
                row.get_by_role("button", name="Play").click()
                print("    Clicked Play.")
                played += 1
            except Exception as e:
                print(f"    Skipped ({e})")
                continue

            if played < count:
                wait_s = max(interval, MIN_INTERVAL_SECONDS)
                print(f"    Waiting {wait_s}s for cooldown...")
                time.sleep(wait_s)

        context.storage_state(path=str(SESSION_FILE))
        browser.close()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Find and play MFL friendlies against similarly-rated clubs.")
    parser.add_argument("--build-index", action="store_true",
                         help="One-time (or refresh) scan of all club ids into club_index.json, then exit")
    parser.add_argument("--club-id", type=int, help="Your club's numeric id (from the app.playmfl.com/clubs/<id> URL)")
    parser.add_argument("--formation", default="4-3-3", choices=list(FORMATIONS.keys()))
    parser.add_argument("--tolerance", type=float, default=3.0, help="Max OVR gap to count as 'similar'")
    parser.add_argument("--division-radius", type=int, default=1,
                         help="How many divisions above/below yours to consider as candidates")
    parser.add_argument("--count", type=int, default=1, help="How many friendlies to play this run")
    parser.add_argument("--interval", type=int, default=MIN_INTERVAL_SECONDS,
                         help=f"Seconds between friendlies (min {MIN_INTERVAL_SECONDS})")
    parser.add_argument("--dry-run", action="store_true", help="Only find/print candidates, don't play anything")
    args = parser.parse_args()

    if args.build_index:
        build_index()
        sys.exit(0)

    if not args.club_id:
        parser.error("--club-id is required (unless using --build-index)")

    if args.interval < MIN_INTERVAL_SECONDS:
        print(f"Note: raising --interval to the {MIN_INTERVAL_SECONDS}s cooldown minimum.")
        args.interval = MIN_INTERVAL_SECONDS

    my_rating, matches = find_similar_opponents(
        args.club_id, args.formation, args.tolerance, args.division_radius
    )

    if not matches:
        print("\nNo similarly-rated opponents found. Try a larger --tolerance or --division-radius.")
        sys.exit(0)

    print(f"\nFound {len(matches)} candidate(s) within {args.tolerance} OVR:")
    for m in matches[:20]:
        print(f"  {m['name']:30s} rating {m['rating']:.1f}  gap {m['gap']:.1f}")

    if args.dry_run:
        sys.exit(0)

    play_friendlies(args.club_id, matches, args.count, args.interval)
