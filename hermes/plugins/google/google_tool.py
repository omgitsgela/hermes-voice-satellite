"""Google Workspace access for Hermes, as a plugin tool, backed by Angela's `gog` CLI.

Why this exists as a plugin rather than the documented CLI workflow: `gog` is a command-line tool,
so reaching it needs the `terminal` toolset, which the voice surface deliberately lacks. That is
exactly why a spoken "what's on my backlog tasks?" failed on 2026-09-22: the agent read the Google
skill, learned it had to run `gog tasks ...`, and had no way to run anything.

Safety model, for a microphone in a shared living space:

* Only the subcommands in `ALLOWED` are ever invoked, and every call ALSO passes gog's own
  ``--enable-commands-exact`` allowlist, so a bug in this file still cannot reach anything outside
  that set. Two independent gates.
* Arguments are built as an argv list with ``shell=False``, never a shell string. Nothing spoken,
  and nothing read from a web page, can reach a command line.
* Every argument is validated by shape and length. There are no path or file arguments here at all.
* There is no delete action. Tasks can be added and completed; nothing is removed.
* Angela's policy, 2026-09-22, recorded so a future session does not "helpfully" re-add it:
  finishing an individual task is low risk and is allowed; **clearing or deleting a list is not**.
  So no such action exists here, and gog's own allowlist refuses `tasks delete` and `tasks clear`
  (verified by probe). `tasks lists` has no delete or clear subcommand in gog v0.29.0 at all, and if
  a future version adds one it will not be in the allowlist above.
* Calendar writes require a summary, a start and an end. A missing timezone offset is filled from
  this host's local zone, because Google rejects a naive datetime.
* "today" and "tomorrow" are resolved from the host clock, never from the model's sense of the date.
* Event summaries deliberately omit event descriptions, which are free text an event organiser
  controls: a voice reply should not read untrusted text aloud, and a title and time is what a
  person actually wants to hear.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any, Optional

GOG_ENV = "HERMES_GOG_BIN"
TIMEOUT = 25.0
MAX_ITEMS = 12          # per reply: this is spoken, not scrolled
MAX_TITLE = 120
MAX_NOTES = 400
MAX_LIST = 80
MAX_QUERY = 64
MAX_NEAR = 80

# Places lookups. The key is read from its file here, never logged, never returned to the caller and
# never passed on a command line.
PLACES_KEY_FILE = "/home/angela/google-places-api-key.txt"
PLACES_FIELDS = ("places.id,places.displayName,places.formattedAddress,places.location,"
                 "places.regularOpeningHours,places.businessStatus")
PLACES_TIMEOUT = 20.0
PLACES_RADIUS_M = 20000.0     # what "nearby" means to a person, not a metro area
MAX_PLACES = 5                # a spoken answer: a shortlist beats a directory
# Angela's home, 5250 E Cherry Creek S Dr, Denver 80246 (canonical record: the vault Housing note;
# see also the local-service-location-research skill).
HOME_LAT, HOME_LNG = 39.699274, -104.928402

# The only gog subcommands this tool will ever run, as this module invokes them. Checked here,
# and handed to gog as well, so a bug in either place is caught by the other.
ALLOWED = (
    ("tasks", "lists"),
    ("tasks", "list"),
    ("tasks", "add"),
    ("tasks", "complete"),
    ("calendar", "calendars"),
    ("calendar", "list"),
    ("calendar", "add"),
    ("contacts", "list"),
    ("maps", "geocode"),
)

# The same set as GOG names them, for --enable-commands-exact. Verified against gog v0.29.0 by
# running each command under a restrictive flag and reading the "command ... is not enabled"
# message, which names what gog actually tried to run. Two names are surprising enough to write
# down: `tasks complete` is internally `tasks.done`, and `calendar add` is `calendar.create`.
# `tasks delete` and `tasks clear` are deliberately absent and confirmed refused.
# Re-verify with that probe after any gog upgrade.
# `maps.geocode` is here to resolve a named origin. Places itself does NOT go through gog: its
# `maps places search` returns only a name, address and id, and `maps places details` returns no
# opening hours and no coordinates (verified 2026-09-22), which is precisely what a hours question
# needs. Places is called against the v1 API directly by `_places_search` below.
GOG_ALLOWLIST = ",".join((
    "tasks.lists.list", "tasks.list", "tasks.add", "tasks.done",
    "calendar.calendars", "calendar.events", "calendar.create", "contacts.list",
    "maps.geocode",
))


class GoogleError(RuntimeError):
    """Refusals and failures that should reach the caller as a plain message."""


# -- plumbing ---------------------------------------------------------------------------------

def gog_bin() -> str:
    override = os.environ.get(GOG_ENV, "").strip()
    if override and os.path.exists(override):
        return override
    found = shutil.which("gog")
    if found:
        return found
    for candidate in ("~/.local/bin/gog", "/usr/local/bin/gog", "/usr/bin/gog"):
        path = os.path.expanduser(candidate)
        if os.path.exists(path):
            return path
    raise GoogleError("the gog CLI was not found on this host")


def run_gog(args: list[str], *, json_out: bool = True) -> Any:
    """Run one allowlisted gog invocation and return its output."""
    pair = tuple(args[:2])
    if pair not in ALLOWED:
        raise GoogleError(f"refused: {' '.join(pair)} is not an allowed operation")
    command = [gog_bin(), f"--enable-commands-exact={GOG_ALLOWLIST}", *args]
    if json_out:
        command += ["--json", "--results-only"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT,
                              shell=False)  # noqa: S603 - fixed argv, no shell
    except subprocess.TimeoutExpired as exc:
        raise GoogleError(f"gog did not answer within {TIMEOUT:.0f} seconds") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:200]
        if "invalid_grant" in detail or "unauthorized" in detail.lower():
            raise GoogleError("Google authentication is not working, so nothing can be verified")
        raise GoogleError(f"gog failed: {detail}")
    out = proc.stdout.strip()
    if not json_out:
        return out
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise GoogleError(f"gog returned something unreadable: {out[:100]!r}") from exc


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line miles between two points, which is what a person means by "nearby"."""
    radius = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _places_key() -> str:
    try:
        with open(PLACES_KEY_FILE, encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError as exc:
        raise GoogleError("the Places API key file is not readable") from exc
    if not key:
        raise GoogleError("the Places API key file is empty")
    return key


def _places_post(path: str, body: dict) -> Any:
    """One Places v1 call. The key rides in a header, never in a URL, a log line or a message."""
    request = urllib.request.Request(
        f"https://places.googleapis.com/v1/{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Goog-Api-Key": _places_key(),
                 "X-Goog-FieldMask": PLACES_FIELDS},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=PLACES_TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - a readable error must not cost the whole call
            pass
        if exc.code in (401, 403):
            raise GoogleError("the Places API refused the key") from exc
        raise GoogleError(f"the Places API returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GoogleError("the Places API did not answer") from exc


def _geocode(value: str) -> Optional[tuple[float, float]]:
    """Resolve a named origin to coordinates, using gog's geocoder."""
    data = run_gog(["maps", "geocode", value])
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return None
    location = ((results[0] or {}).get("geometry") or {}).get("location") or {}
    lat, lng = location.get("lat"), location.get("lng")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def _places_search(args: dict) -> str:
    """What is near home: names, addresses, distances and real opening hours.

    Biased to Angela's home unless another origin is named, because "near me" from this house means
    near 5250 E Cherry Creek S Dr, not near her job in Golden.
    """
    query = _clean(args.get("query"), field="query", max_len=MAX_QUERY)
    near = _clean(args.get("near"), field="near", max_len=MAX_NEAR, required=False)
    label, origin = "home", (HOME_LAT, HOME_LNG)
    if near and near.strip().lower() not in ("home", "here", "my house", "my home"):
        found = _geocode(near)
        if found is None:
            raise GoogleError(f"could not locate {near!r} to search around")
        label, origin = near, found

    data = _places_post("places:searchText", {
        "textQuery": query,
        "maxResultCount": MAX_PLACES,
        "locationBias": {"circle": {"center": {"latitude": origin[0], "longitude": origin[1]},
                                    "radius": PLACES_RADIUS_M}},
    })
    places = []
    for item in (data.get("places") or [])[:MAX_PLACES]:
        location = item.get("location") or {}
        hours = item.get("regularOpeningHours") or {}
        distance = None
        if location.get("latitude") is not None and location.get("longitude") is not None:
            distance = round(_haversine_miles(origin[0], origin[1],
                                              location["latitude"], location["longitude"]), 1)
        places.append({
            "name": str((item.get("displayName") or {}).get("text") or "").strip(),
            "address": str(item.get("formattedAddress") or "").strip(),
            "distance_miles": distance,
            "open_now": hours.get("openNow"),
            "hours": [str(line) for line in (hours.get("weekdayDescriptions") or [])],
            "status": item.get("businessStatus"),
            "place_id": item.get("id"),
        })
    return _ok(query=query, near=label, count=len(places), places=places)


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _clean(value: Any, *, field: str, max_len: int = MAX_TITLE, required: bool = True) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        if required:
            raise GoogleError(f"{field} is required")
        return ""
    if len(text) > max_len:
        raise GoogleError(f"{field} is too long (limit {max_len})")
    return text


def _rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        items = data.get("items") or data.get("tasks") or []
        return [row for row in items if isinstance(row, dict)]
    return []


# -- dates and times --------------------------------------------------------------------------

def _local_offset() -> str:
    raw = dt.datetime.now().astimezone().strftime("%z") or "+0000"
    return f"{raw[:3]}:{raw[3:]}"


def _resolve_date(value: str, *, field: str) -> str:
    """YYYY-MM-DD, or a word resolved from the host clock."""
    word = value.strip().lower()
    today = dt.date.today()
    if word in ("today", "now"):
        return today.isoformat()
    if word == "tomorrow":
        return (today + dt.timedelta(days=1)).isoformat()
    if word == "yesterday":
        return (today - dt.timedelta(days=1)).isoformat()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", word)
    if not match:
        raise GoogleError(f"{field} must be a date like 2026-09-22, or today or tomorrow")
    try:
        return dt.date(*map(int, match.groups())).isoformat()
    except ValueError as exc:
        raise GoogleError(f"{field} is not a real date: {value}") from exc


def _resolve_datetime(value: str, *, field: str) -> str:
    """A datetime with an offset, filling the offset in from this host when it is missing."""
    text = value.strip()
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?",
                         text)
    if not match:
        # Accept a bare date only to give a better message.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise GoogleError(f"{field} needs a time as well, like {text}T14:00")
        raise GoogleError(f"{field} must look like 2026-09-22T14:00")
    date, hour, minute, second, offset = match.groups()
    seconds = second or "00"
    if offset:
        zone = offset if offset == "Z" else (offset if ":" in offset else f"{offset[:3]}:{offset[3:]}")
    else:
        zone = _local_offset()
    return f"{date}T{int(hour):02d}:{minute}:{seconds}{zone}"


def _short_when(value: str) -> str:
    """A compact local time for speaking, e.g. 'Tue 11:00 AM'."""
    if not value:
        return ""
    text = value.strip()
    try:
        if "T" in text:
            moment = dt.datetime.fromisoformat(text)
            if moment.tzinfo is not None:
                moment = moment.astimezone()          # show it in Angela's own timezone
            return moment.strftime("%a %-I:%M %p")
        return dt.date.fromisoformat(text).strftime("%a (all day)")
    except ValueError:
        return text


# -- lookups -----------------------------------------------------------------------------------

def _task_lists() -> list[dict]:
    return _rows(run_gog(["tasks", "lists"]))


def _tasks_in(list_id: str) -> list[dict]:
    return _rows(run_gog(["tasks", "list", list_id, "--all"]))


def _calendars() -> list[dict]:
    return _rows(run_gog(["calendar", "calendars"]))


def _calendar_index() -> dict[str, str]:
    return {str(c.get("summary") or ""): str(c.get("id") or "") for c in _calendars()}


def _pick(items: list[dict], query: str, *, key: str, what: str) -> dict:
    """Match one item by name. Ambiguity is refused rather than guessed.

    Refusing matters more than being helpful here: completing the wrong task or filing an event on
    the wrong calendar is a real mistake, and a voice channel cannot show a picker.
    """
    wanted = query.strip().lower()
    exact = [i for i in items if str(i.get(key) or "").strip().lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    partial = [i for i in items if wanted in str(i.get(key) or "").strip().lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        options = ", ".join(sorted(str(i.get(key)) for i in items)[:8])
        raise GoogleError(f"nothing matches {query!r} for {what}; available: {options}")
    options = ", ".join(str(i.get(key)) for i in partial[:5])
    raise GoogleError(f"{query!r} matches more than one {what}: {options}")


def _default_task_list() -> dict:
    """Angela's convention: unspecified work goes to My Tasks."""
    lists = _task_lists()
    for entry in lists:
        if str(entry.get("title") or "").strip().lower() == "my tasks":
            return entry
    if len(lists) == 1:
        return lists[0]
    raise GoogleError("could not find a 'My Tasks' list; name the list explicitly")


def _resolve_task_list(name: str | None) -> dict:
    if not name:
        return _default_task_list()
    return _pick(_task_lists(), name, key="title", what="task lists")


# -- actions -----------------------------------------------------------------------------------

def _tasks_lists() -> str:
    lists = _task_lists()
    return _ok(lists=[str(entry.get("title")) for entry in lists])


def _tasks_list(args: dict) -> str:
    listing = _resolve_task_list(args.get("list"))
    items = [t for t in _tasks_in(str(listing.get("id"))) if t.get("status") != "completed"]
    today = dt.date.today().isoformat()
    rows = [{"title": str(t.get("title") or ""), "due": (t.get("due") or "")[:10] or None}
            for t in items[:MAX_ITEMS]]
    due = [str(t.get("title")) for t in items
           if (t.get("due") or "")[:10] and (t.get("due") or "")[:10] <= today]
    return _ok(list=str(listing.get("title")), open=len(items), tasks=rows,
               more=max(0, len(items) - MAX_ITEMS), due_today_or_overdue=due[:5])


def _task_add(args: dict) -> str:
    # Validate everything the caller supplied BEFORE any gog call, so a bad request fails fast and
    # locally instead of after a wasted round trip. (An early version resolved the list first, so a
    # bogus due date was only caught after two gog invocations.)
    title = _clean(args.get("title"), field="title")
    due = _resolve_date(str(args["due"]), field="due") if args.get("due") else None
    notes = _clean(args["notes"], field="notes", max_len=MAX_NOTES) if args.get("notes") else None
    listing = _resolve_task_list(args.get("list"))
    argv = ["tasks", "add", str(listing.get("id")), f"--title={title}"]
    if due:
        argv.append(f"--due={due}")
    if notes:
        argv.append(f"--notes={notes}")
    run_gog(argv)
    after = _tasks_in(str(listing.get("id")))
    match = [t for t in after if str(t.get("title") or "").strip().lower() == title.lower()]
    return _ok(list=str(listing.get("title")), added=title, verified=bool(match))


def _task_complete(args: dict) -> str:
    query = _clean(args.get("title"), field="title")   # validate before any gog call
    listing = _resolve_task_list(args.get("list"))
    open_items = [t for t in _tasks_in(str(listing.get("id"))) if t.get("status") != "completed"]
    target = _pick(open_items, query, key="title", what="open tasks")
    run_gog(["tasks", "complete", str(listing.get("id")), str(target.get("id")), "-y"])
    after = [t for t in _tasks_in(str(listing.get("id"))) if str(t.get("id")) == str(target.get("id"))]
    done = bool(after) and after[0].get("status") == "completed"
    return _ok(list=str(listing.get("title")), completed=str(target.get("title")), verified=done)


def _calendar_rows(args: dict, *, default_days: int) -> tuple[str, str, list[dict]]:
    start = _resolve_date(str(args.get("from") or "today"), field="from")
    if args.get("to"):
        end = _resolve_date(str(args["to"]), field="to")
    else:
        end = (dt.date.fromisoformat(start) + dt.timedelta(days=default_days)).isoformat()
    argv = ["calendar", "list", "--all", f"--from={start}", f"--to={end}",
            "--all-pages", "--max", "100"]
    if args.get("query"):
        argv.append(f"--query={_clean(args['query'], field='query', max_len=MAX_QUERY)}")
    events = _rows(run_gog(argv))
    names = _calendar_index()
    rows = []
    for event in events:
        start_block = event.get("start") or {}
        when = str(start_block.get("dateTime") or start_block.get("date") or "")
        rows.append({"when": _short_when(when),
                     "title": str(event.get("summary") or "(no title)"),
                     "calendar": names.get(str(event.get("CalendarID") or ""), "")})
    rows.sort(key=lambda row: row["when"])
    return start, end, rows


def _calendar_today(args: dict) -> str:
    start, end, rows = _calendar_rows(args, default_days=1)
    return _ok(from_date=start, to_date=end, count=len(rows), events=rows[:MAX_ITEMS],
               more=max(0, len(rows) - MAX_ITEMS))


def _calendar_list(args: dict) -> str:
    start, end, rows = _calendar_rows(args, default_days=1)
    return _ok(from_date=start, to_date=end, count=len(rows), events=rows[:MAX_ITEMS],
               more=max(0, len(rows) - MAX_ITEMS))


def _calendar_calendars() -> str:
    entries = _calendars()
    return _ok(calendars=[str(c.get("summary")) for c in entries])


def _event_add(args: dict) -> str:
    summary = _clean(args.get("summary"), field="summary")
    start = _resolve_datetime(str(args.get("from") or ""), field="from")
    end = _resolve_datetime(str(args.get("to") or ""), field="to")
    if dt.datetime.fromisoformat(end) <= dt.datetime.fromisoformat(start):
        raise GoogleError("the end time must be after the start time")
    wanted = _clean(args.get("calendar") or "primary", field="calendar", max_len=MAX_LIST)
    calendars = _calendars()
    if wanted.lower() in ("primary", "personal"):
        target = next((c for c in calendars if "personal" in str(c.get("summary") or "").lower()),
                      calendars[0] if calendars else None)
        if target is None:
            raise GoogleError("no calendars are available")
    else:
        target = _pick(calendars, wanted, key="summary", what="calendars")
    argv = ["calendar", "add", str(target.get("id")), f"--summary={summary}",
            f"--from={start}", f"--to={end}"]
    if args.get("location"):
        argv.append(f"--location={_clean(args['location'], field='location', max_len=MAX_LIST)}")
    if args.get("description"):
        argv.append(f"--description={_clean(args['description'], field='description', max_len=MAX_NOTES)}")
    run_gog(argv)
    found = _rows(run_gog(["calendar", "list", str(target.get("id")), f"--from={start[:10]}",
                           f"--to={end[:10]}", "--all-pages", "--max", "100"]))
    titled = [e for e in found if str(e.get("summary") or "").strip().lower() == summary.lower()]
    return _ok(calendar=str(target.get("summary")), summary=summary, from_time=start, to_time=end,
               verified=bool(titled))


def _contacts_search(args: dict) -> str:
    query = _clean(args.get("query"), field="query", max_len=MAX_QUERY).lower()
    text = run_gog(["contacts", "list", "--plain"], json_out=False)
    hits = []
    for line in str(text).splitlines()[1:]:                      # first line is a TSV header
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        name, email, phone = fields[1], fields[2], fields[3]
        if query in name.lower() or query in email.lower() or query in phone.lower():
            hits.append({"name": name, "email": email, "phone": phone})
    return _ok(query=query, count=len(hits), contacts=hits[:MAX_ITEMS],
               more=max(0, len(hits) - MAX_ITEMS))


_ACTIONS = {
    "tasks_lists": _tasks_lists,
    "tasks_list": _tasks_list,
    "task_add": _task_add,
    "task_complete": _task_complete,
    "calendar_today": _calendar_today,
    "calendar_list": _calendar_list,
    "calendar_calendars": _calendar_calendars,
    "event_add": _event_add,
    "contacts_search": _contacts_search,
    "places_search": _places_search,
}


def handle(args: dict) -> str:
    """Tool entry point. Always returns a JSON string, as tool handlers must."""
    action = str(args.get("action") or "").strip().lower()
    handler = _ACTIONS.get(action)
    if handler is None:
        return _fail(f"unknown action {action!r}; known: {', '.join(sorted(_ACTIONS))}")
    try:
        if action in ("tasks_lists", "calendar_calendars"):
            return handler()                      # type: ignore[call-arg]
        return handler(args)                      # type: ignore[arg-type]
    except GoogleError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool must never raise into the agent
        return _fail(f"{type(exc).__name__}: {exc}")
