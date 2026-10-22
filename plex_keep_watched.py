#!/usr/bin/env python3
"""
plex-keep-watched

Keeps already-watched Plex movies marked as "watched" when you only start them
for a moment (misclick, quick check, jump around). Plex turns such a movie back
into "in progress" as soon as it stores a resume position; this script puts it
back to "watched" afterwards.

Two rules, evaluated on every run:
  - Short play (misclick, quick look): marked watched again on this run.
  - Real watch: marked watched again only if the resume position has not moved
    for delay_hours hours. If you resume the movie, the countdown restarts.

What counts as "short":
  - Without Tautulli: a resume position below short_play_minutes.
  - With Tautulli (optional): the time actually played, so that "I jumped to
    minute 42 and watched 30 seconds" is not mistaken for a real watch. If
    Tautulli knows nothing about the movie or does not answer, the script falls
    back to the resume position rule.
    ATTENTION, prerequisite: set Tautulli's "Logging Ignore Interval" to
    1 second (Settings > General), otherwise plays shorter than 2 minutes are
    not recorded (see config.example.ini).

Anything that is playing (or paused in a player) right now is never touched.

Settings live in a config file, config.ini next to the script by default (copy
config.example.ini, every setting is explained there). Order of precedence:
command-line options, then environment variables (PLEX_URL, PLEX_TOKEN,
TAUTULLI_URL, TAUTULLI_API_KEY, KEEP_WATCHED_DATA_DIR, KEEP_WATCHED_CONFIG),
then the config file, then built-in defaults.

Logs, written to the data directory (next to the script by default):
  - keep_watched.log: the last changes, warnings and errors. Can be switched
    off with basic_log = false in the config file.
  - Verbose mode (verbose = true, or --verbose for a manual run):
    keep_watched_verbose.log describes every run, item by item: what is marked
    watched, what is waiting (and until when), what is skipped and why.
Warnings and errors are always printed on stderr as well.

Run it regularly (for example every 30 minutes) with cron or any scheduler.
Use --dry-run first to see what it would do without changing anything.

Only the Plex account that owns the token is handled: "watched" status is
stored per Plex user.
"""
import argparse
import configparser
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.ini")

# Every setting: (section, key) -> (type, default).
# What each one means is documented in config.example.ini and in the README.
SETTINGS = {
    ("plex", "url"): (str, "http://127.0.0.1:32400"),
    ("plex", "token"): (str, ""),
    ("behavior", "short_play_minutes"): (float, 5),
    ("behavior", "delay_hours"): (float, 48),
    ("behavior", "include_episodes"): (bool, False),
    ("tautulli", "url"): (str, ""),
    ("tautulli", "api_key"): (str, ""),
    ("logging", "basic_log"): (bool, True),
    ("logging", "verbose"): (bool, False),
    ("logging", "max_lines"): (int, 100),
    ("logging", "verbose_max_lines"): (int, 2000),
    ("files", "data_dir"): (str, ""),
}

# Environment variables that override the config file.
ENV_OVERRIDES = {
    ("plex", "url"): "PLEX_URL",
    ("plex", "token"): "PLEX_TOKEN",
    ("tautulli", "url"): "TAUTULLI_URL",
    ("tautulli", "api_key"): "TAUTULLI_API_KEY",
    ("files", "data_dir"): "KEEP_WATCHED_DATA_DIR",
}

PAGE = 500
TOKEN_PLACEHOLDERS = ("", "YOUR_PLEX_TOKEN")
DRY_RUN = False             # set by --dry-run
SETTINGS_LOADED = False     # True once load_settings() succeeded (log files may be written)
UNWRITABLE_LOGS = set()     # log files already reported as unwritable


# ---- Settings --------------------------------------------------------------

def default_values():
    return {setting: spec[1] for setting, spec in SETTINGS.items()}


def apply_settings(values):
    """Copy a {(section, key): value} dict into the module-level settings."""
    global PLEX_URL, PLEX_TOKEN, SHORT_PLAY_MINUTES, DELAY_HOURS, INCLUDE_EPISODES
    global TAUTULLI_URL, TAUTULLI_API_KEY, BASIC_LOG, VERBOSE, LOG_MAX_LINES, VERBOSE_LOG_MAX_LINES
    PLEX_URL = values[("plex", "url")]
    PLEX_TOKEN = values[("plex", "token")]
    SHORT_PLAY_MINUTES = values[("behavior", "short_play_minutes")]
    DELAY_HOURS = values[("behavior", "delay_hours")]
    INCLUDE_EPISODES = values[("behavior", "include_episodes")]
    TAUTULLI_URL = values[("tautulli", "url")]
    TAUTULLI_API_KEY = values[("tautulli", "api_key")]
    BASIC_LOG = values[("logging", "basic_log")]
    VERBOSE = values[("logging", "verbose")]
    LOG_MAX_LINES = values[("logging", "max_lines")]
    VERBOSE_LOG_MAX_LINES = values[("logging", "verbose_max_lines")]


def apply_data_dir(path):
    """Where the state and log files live (the script's folder when path is empty)."""
    global DATA_DIR, STATE_FILE, LOG_FILE, VERBOSE_LOG_FILE
    DATA_DIR = os.path.abspath(os.path.expanduser(path)) if path else SCRIPT_DIR
    STATE_FILE = os.path.join(DATA_DIR, "keep_watched_state.json")
    LOG_FILE = os.path.join(DATA_DIR, "keep_watched.log")
    VERBOSE_LOG_FILE = os.path.join(DATA_DIR, "keep_watched_verbose.log")


def parse_value(section, key, raw, kind):
    """Convert one raw config value. An empty value means "use the default" (None)."""
    raw = raw.strip()
    if raw == "":
        return None
    where = f"[{section}] {key}"
    if kind is bool:
        if raw.lower() in ("1", "yes", "true", "on"):
            return True
        if raw.lower() in ("0", "no", "false", "off"):
            return False
        raise RuntimeError(f"invalid yes/no value for {where}: {raw!r} (use true or false)")
    if kind is int:
        try:
            number = int(raw)
        except ValueError:
            raise RuntimeError(f"invalid whole number for {where}: {raw!r}") from None
        if number < 1:
            raise RuntimeError(f"{where} must be at least 1 (got {raw})")
        return number
    if kind is float:
        try:
            number = float(raw)
        except ValueError:
            raise RuntimeError(f"invalid number for {where}: {raw!r}") from None
        if number < 0:
            raise RuntimeError(f"{where} must not be negative (got {raw})")
        return number
    return raw


def read_config(path):
    """Read and validate a config file. Returns {(section, key): value} for the keys that are set."""
    parser = configparser.ConfigParser(interpolation=None)   # no % interpolation: tokens may contain %
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as exc:
        raise RuntimeError(f"cannot read config file {path}: {exc}") from None
    found = {}
    for section in parser.sections():
        for key, raw in parser.items(section):
            if (section, key) not in SETTINGS:
                raise RuntimeError(f"unknown setting [{section}] {key} in {path} "
                                   f"(check the spelling against config.example.ini)")
            value = parse_value(section, key, raw, SETTINGS[(section, key)][0])
            if value is not None:
                found[(section, key)] = value
    return found


def load_settings(config_path=None):
    """Resolve the settings: defaults < config file < environment variables."""
    explicit = config_path or os.environ.get("KEEP_WATCHED_CONFIG")
    path = explicit or DEFAULT_CONFIG_PATH
    values = default_values()
    if os.path.exists(path):
        values.update(read_config(path))
    elif explicit:
        raise RuntimeError(f"config file not found: {path}")
    for setting, env_name in ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            values[setting] = env_value
    apply_settings(values)
    apply_data_dir(values[("files", "data_dir")])
    global SETTINGS_LOADED
    SETTINGS_LOADED = True


# Built-in defaults until load_settings() runs (and for anyone importing the module)
apply_settings(default_values())
apply_data_dir(os.environ.get("KEEP_WATCHED_DATA_DIR", ""))


# ---- Logging ---------------------------------------------------------------

def out(line, error=False):
    """print() that never crashes on a non-UTF-8 terminal (some cron setups)."""
    stream = sys.stderr if error else sys.stdout
    try:
        print(line, file=stream)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), file=stream)


def write_line(path, line, max_lines):
    """
    Append a line to a log file and keep only the last max_lines lines.
    A log file that cannot be written (permissions, full disk...) must never
    hide the real problem or stop the job: it is reported once on stderr.
    """
    try:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        lines.append(line)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-max_lines:]) + "\n")
    except OSError as exc:
        if path not in UNWRITABLE_LOGS:
            UNWRITABLE_LOGS.add(path)
            out(stamp(f"WARNING: cannot write the log file {path}: {exc}"), error=True)


def stamp(message):
    return f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"


def log(message, level="info"):
    """
    Main log: changes, warnings, errors. Always printed (warnings and errors on
    stderr); written to keep_watched.log unless disabled; copied to the verbose
    log when verbose is on.
    """
    line = stamp(message)
    out(line, error=(level != "info"))
    if DRY_RUN or not SETTINGS_LOADED:
        return      # before the settings are known we cannot tell where logs belong
    if BASIC_LOG:
        write_line(LOG_FILE, line, LOG_MAX_LINES)
    if VERBOSE:
        write_line(VERBOSE_LOG_FILE, line, VERBOSE_LOG_MAX_LINES)


def vlog(message):
    """Detailed log: does nothing unless verbose (or a dry run, which only prints)."""
    if not (VERBOSE or DRY_RUN):
        return
    line = stamp(message)
    out(line)
    if DRY_RUN:
        return
    write_line(VERBOSE_LOG_FILE, line, VERBOSE_LOG_MAX_LINES)


# ---- Plex ------------------------------------------------------------------

def plex(path, params=None):
    params = dict(params or {})
    params["X-Plex-Token"] = PLEX_TOKEN
    url = f"{PLEX_URL.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = resp.read()
    # Some actions (e.g. /:/scrobble) answer with an empty body
    return ET.fromstring(body) if body.strip() else None


def sections():
    root = plex("/library/sections")
    for d in root.findall("Directory"):
        kind = d.get("type")
        if kind == "movie":
            yield d.get("key"), "1", "Video"
        elif kind == "show" and INCLUDE_EPISODES:
            yield d.get("key"), "4", "Video"


def playing_now():
    """ratingKey of everything playing or paused on the server right now."""
    root = plex("/status/sessions")
    return {v.get("ratingKey") for v in root.findall("Video")}


def in_progress_items(stats=None):
    """Items watched at least once AND currently holding a resume position."""
    for key, type_id, tag in sections():
        start = 0
        while True:
            root = plex(f"/library/sections/{key}/all", {
                "type": type_id,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE,
            })
            items = root.findall(tag)
            if stats is not None:
                stats["scanned"] += len(items)
            for it in items:
                if int(it.get("viewCount", 0)) > 0 and int(it.get("viewOffset", 0)) > 0:
                    yield it.get("ratingKey"), it.get("title", "?"), int(it.get("viewOffset"))
            total = int(root.get("totalSize", 0))
            start += PAGE
            if start >= total or not items:
                break


# ---- Tautulli (optional) ---------------------------------------------------

def tautulli_enabled():
    return bool(TAUTULLI_URL.strip() and TAUTULLI_API_KEY.strip())


def tautulli_history(rating_key):
    """Plays recorded by Tautulli for this item, most recent first."""
    params = {
        "apikey": TAUTULLI_API_KEY.strip(),
        "cmd": "get_history",
        "rating_key": rating_key,
        "grouping": 0,              # one row per play, no grouping
        "order_column": "date",
        "order_dir": "desc",
        "length": 50,
    }
    url = f"{TAUTULLI_URL.strip().rstrip('/')}/api/v2?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        payload = json.load(resp)
    response = payload.get("response", {})
    if response.get("result") != "success":
        raise RuntimeError(response.get("message") or "error response from Tautulli")
    data = response.get("data", [])
    return data.get("data", []) if isinstance(data, dict) else data


def real_play_seconds(rating_key, delay_seconds):
    """
    Time actually played in the current viewing, according to Tautulli.
    Plays are walked from the most recent to the oldest and added up as long as
    they belong to the same viewing: we stop at a completed viewing, or when two
    plays are more than delay_hours apart.
    Returns None when Tautulli has nothing usable for this item.
    """
    total = 0
    counted = 0
    prev_started = None
    for row in tautulli_history(rating_key):
        started = int(row.get("started") or 0)
        stopped = int(row.get("stopped") or 0)
        if prev_started is not None and prev_started - stopped > delay_seconds:
            break
        if row.get("watched_status") == 1:
            break
        total += int(row.get("play_duration") or 0)
        counted += 1
        prev_started = started
    return total if counted else None


# ---- State -----------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def fmt_position(offset_ms):
    minutes, seconds = divmod(offset_ms // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


# ---- Main ------------------------------------------------------------------

def main():
    if PLEX_TOKEN in TOKEN_PLACEHOLDERS:
        raise RuntimeError("Plex token is not set: put it in config.ini ([plex] token) "
                           "or set the PLEX_TOKEN environment variable")

    now = time.time()
    delay = DELAY_HOURS * 3600
    short_seconds = SHORT_PLAY_MINUTES * 60
    old = load_state()
    new = {}
    playing = playing_now()
    use_tautulli = tautulli_enabled()
    stats = {"scanned": 0}
    counts = {"candidates": 0, "reset": 0, "waiting": 0, "skipped": 0}

    vlog(f"=== RUN{' (dry run: nothing will be changed)' if DRY_RUN else ''}: "
         f"short-play threshold {SHORT_PLAY_MINUTES:g} min, delay {DELAY_HOURS:g} h, "
         f"Tautulli {'enabled' if use_tautulli else 'disabled'}, "
         f"{len(playing)} playback(s) in progress on Plex")

    for rating_key, title, offset in in_progress_items(stats):
        counts["candidates"] += 1
        label = f"'{title}' (id {rating_key}) - position {fmt_position(offset)}"

        # Playing or paused right now: leave it alone, the countdown restarts
        if rating_key in playing:
            new[rating_key] = {"offset": offset, "since": now}
            counts["skipped"] += 1
            vlog(f"SKIPPED  {label}: playing or paused, countdown reset")
            continue

        prev = old.get(rating_key)
        # Same position as on the previous run: keep the first-seen time
        if prev and prev["offset"] == offset:
            since = prev["since"]
            note = "position unchanged since the previous run"
        elif prev:
            since = now
            note = "position changed since the previous run, countdown reset"
        else:
            since = now
            note = "first seen, countdown started"

        # Time actually played (Tautulli), or None -> fall back on the position
        played = None
        source = f"resume position {fmt_position(offset)}"
        if use_tautulli:
            try:
                played = real_play_seconds(rating_key, delay)
            except Exception as exc:
                use_tautulli = False   # no point retrying for the other items
                log(f"WARNING: Tautulli unusable ({exc}), falling back to the resume position.",
                    level="warning")
            else:
                if played is None:
                    source += " (Tautulli: no usable history)"
        if played is not None:
            source = f"{fmt_position(played * 1000)} actually played (Tautulli)"
            is_short = played < short_seconds
        else:
            is_short = offset < short_seconds * 1000

        if is_short:
            reason = "short play"
            if played is not None:
                reason += f", {fmt_position(played * 1000)} actually played"
        elif now - since >= delay:
            reason = f"inactive for {int((now - since) / 3600)} h"
        else:
            due = since + delay
            new[rating_key] = {"offset": offset, "since": since}
            counts["waiting"] += 1
            vlog(f"WAITING  {label}: real watch (based on {source} >= {SHORT_PLAY_MINUTES:g} min); "
                 f"{note}; will be marked watched from "
                 f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(due))} "
                 f"(in {fmt_duration(due - now)})")
            continue

        counts["reset"] += 1
        if DRY_RUN:
            log(f"[DRY RUN] WOULD MARK AS WATCHED ({reason}): '{title}' (id {rating_key}) - "
                f"stopped at {fmt_position(offset)}")
        else:
            plex("/:/scrobble", {
                "key": rating_key,
                "identifier": "com.plexapp.plugins.library",
            })
            log(f"MARKED AS WATCHED ({reason}): '{title}' (id {rating_key}) - "
                f"stopped at {fmt_position(offset)}")

    if not DRY_RUN:
        save_state(new)
    verb = "would be marked watched" if DRY_RUN else "marked watched"
    vlog(f"Summary: {stats['scanned']} item(s) scanned, {counts['candidates']} already watched with a "
         f"resume position -> {counts['reset']} {verb}, {counts['waiting']} waiting, "
         f"{counts['skipped']} skipped (playing now)")
    return 0


def cli(argv=None):
    """Command-line entry point. Returns the process exit code."""
    global VERBOSE, DRY_RUN, SETTINGS_LOADED
    SETTINGS_LOADED = False
    UNWRITABLE_LOGS.clear()
    parser = argparse.ArgumentParser(
        description="Keep already-watched Plex movies marked as watched after a brief replay.")
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="config file to use (default: $KEEP_WATCHED_CONFIG, "
                             "else config.ini next to the script)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="show what would be done, change nothing (no Plex change, "
                             "no state or log file written)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="detailed run log (also written to keep_watched_verbose.log)")
    args = parser.parse_args(argv)
    DRY_RUN = args.dry_run
    try:
        load_settings(args.config)
        VERBOSE = VERBOSE or args.verbose
        return main()
    except Exception as exc:  # bad config, connection refused, bad token, ...
        log(f"ERROR: {exc}", level="error")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
