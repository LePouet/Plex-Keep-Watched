"""
Tests for plex_keep_watched.py.

They run against small fake Plex and Tautulli servers started on localhost,
and drive the script the way a user does: through a config file.
No real Plex or Tautulli is needed:

    python3 -m unittest discover -s tests -v
"""
import configparser
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "plex_keep_watched.py")
EXAMPLE_CONFIG = os.path.join(ROOT, "config.example.ini")
HOUR = 3600
MIN_MS = 60_000
ENV_NAMES = ("PLEX_URL", "PLEX_TOKEN", "TAUTULLI_URL", "TAUTULLI_API_KEY",
             "KEEP_WATCHED_DATA_DIR", "KEEP_WATCHED_CONFIG")


class FakeServer:
    """A tiny fake Plex (+ Tautulli) HTTP server."""

    def __init__(self):
        self.films = []            # (ratingKey, title, viewCount, viewOffset in ms)
        self.episodes = []         # same, in a "show" library
        self.playing = set()       # ratingKeys currently in a session
        self.history = {}          # ratingKey -> Tautulli history rows
        self.history_errors = set()
        self.tautulli_key = "SECRET"
        self.scrobbled = []
        self.api_calls = []        # parsed query strings of Tautulli calls
        self.requests = 0          # Plex requests received
        self.tokens_seen = set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._handle(self)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def add(self, key, title, offset_ms, views=1):
        self.films.append((key, title, views, offset_ms))

    @staticmethod
    def _videos(items):
        return "".join(
            (f'<Video ratingKey="{k}" title="{t}" viewCount="{c}" viewOffset="{o}"/>' if o else
             f'<Video ratingKey="{k}" title="{t}" viewCount="{c}"/>')
            for k, t, c, o in items)

    def _handle(self, h):
        u = urlparse(h.path)
        q = parse_qs(u.query)
        status, body = 200, ""
        if not u.path.startswith("/api/v2"):
            self.requests += 1
            self.tokens_seen.update(q.get("X-Plex-Token", []))
        if u.path == "/library/sections":
            body = ('<MediaContainer><Directory key="1" type="movie"/>'
                    '<Directory key="2" type="artist"/><Directory key="3" type="show"/>'
                    '</MediaContainer>')
        elif u.path == "/library/sections/1/all":
            body = (f'<MediaContainer totalSize="{len(self.films)}">'
                    f'{self._videos(self.films)}</MediaContainer>')
        elif u.path == "/library/sections/3/all":
            items = self.episodes if q.get("type") == ["4"] else []
            body = f'<MediaContainer totalSize="{len(items)}">{self._videos(items)}</MediaContainer>'
        elif u.path == "/status/sessions":
            body = "<MediaContainer>" + "".join(
                f'<Video ratingKey="{k}"/>' for k in sorted(self.playing)) + "</MediaContainer>"
        elif u.path == "/:/scrobble":
            self.scrobbled.append(q["key"][0])       # real Plex answers with an empty body
        elif u.path == "/api/v2":
            self.api_calls.append(q)
            key = q["rating_key"][0]
            if q["apikey"] != [self.tautulli_key]:
                out = {"response": {"result": "error", "message": "Invalid apikey", "data": {}}}
            elif key in self.history_errors:
                out = {"response": {"result": "error", "message": "boom", "data": {}}}
            elif key.startswith("N"):   # nested envelope {data: {data: [...]}}
                out = {"response": {"result": "success", "message": None,
                                    "data": {"recordsTotal": 1, "data": self.history.get(key, [])}}}
            else:                       # flat envelope {data: [...]}
                out = {"response": {"result": "success", "message": None,
                                    "data": self.history.get(key, [])}}
            body = json.dumps(out)
        else:
            status = 404
        h.send_response(status)
        h.end_headers()
        h.wfile.write(body.encode("utf-8"))


REPO_FILES_BEFORE = set(os.listdir(ROOT))


def tearDownModule():
    """The tests must never leave logs or state files in the repository folder."""
    leftovers = set(os.listdir(ROOT)) - REPO_FILES_BEFORE - {"__pycache__"}
    if leftovers:
        raise AssertionError(f"tests left files in the repo folder: {sorted(leftovers)}")


def row(started_hours_ago, play_seconds, watched=0):
    """A Tautulli history row."""
    started = time.time() - started_hours_ago * HOUR
    return {"started": int(started), "stopped": int(started + play_seconds),
            "play_duration": play_seconds, "watched_status": watched}


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def load_module():
    spec = importlib.util.spec_from_file_location("plex_keep_watched_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)       # restored after each test
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.config_path = os.path.join(self.dir, "config.ini")
        self.state_file = os.path.join(self.dir, "keep_watched_state.json")
        self.log_file = os.path.join(self.dir, "keep_watched.log")
        self.verbose_file = os.path.join(self.dir, "keep_watched_verbose.log")
        self.cfg = {"plex": {"url": self.server.url, "token": "TEST-TOKEN"},
                    "files": {"data_dir": self.dir}}
        self.stderr = ""
        self.m = load_module()
        self.m.apply_data_dir(self.dir)             # even a stray default write lands in the temp dir

    # -- configuration and running -------------------------------------------
    def set(self, section, key, value):
        self.cfg.setdefault(section, {})[key] = str(value)

    def write_config(self):
        lines = []
        for section, items in self.cfg.items():
            lines.append(f"[{section}]")
            lines += [f"{k} = {v}" for k, v in items.items()]
            lines.append("")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def run_raw(self, argv):
        """Run the CLI with exactly these arguments, capturing stdout and stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.m.cli(list(argv))
        self.stderr = err.getvalue()
        return code, out.getvalue()

    def run_script(self, argv=(), write_config=True):
        if write_config:
            self.write_config()
        return self.run_raw(["--config", self.config_path, *argv])

    # -- reading results -----------------------------------------------------
    def read(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def main_log(self):
        return self.read(self.log_file) or ""

    def verbose_log(self):
        return self.read(self.verbose_file)

    def write_state(self, state):
        with open(self.state_file, "w") as f:
            json.dump(state, f)

    def read_state(self):
        with open(self.state_file) as f:
            return json.load(f)


class Defaults(Base):
    def test_built_in_defaults(self):
        m = self.m
        self.assertEqual((m.SHORT_PLAY_MINUTES, m.DELAY_HOURS), (5, 48))
        self.assertFalse(m.INCLUDE_EPISODES)
        self.assertTrue(m.BASIC_LOG)
        self.assertFalse(m.VERBOSE)
        self.assertEqual((m.LOG_MAX_LINES, m.VERBOSE_LOG_MAX_LINES), (100, 2000))
        self.assertFalse(m.tautulli_enabled())


class PlexOnlyRules(Base):
    def test_short_play_is_marked_watched_on_next_run(self):
        self.server.add("1", "Short", 30_000)
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, ["1"])
        self.assertIn("MARKED AS WATCHED (short play)", self.main_log())
        self.assertEqual(self.read_state(), {})

    def test_threshold_is_exclusive(self):
        self.server.add("1", "Exactly five minutes", 5 * MIN_MS)
        self.server.add("2", "Just under", 5 * MIN_MS - 1000)
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["2"])

    def test_long_play_waits_for_the_delay(self):
        self.server.add("1", "Waiting", 50 * MIN_MS)
        self.server.add("2", "Expired", 40 * MIN_MS)
        now = time.time()
        self.write_state({
            "1": {"offset": 50 * MIN_MS, "since": now - 10 * HOUR},
            "2": {"offset": 40 * MIN_MS, "since": now - 49 * HOUR},
        })
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["2"])
        state = self.read_state()
        self.assertEqual(list(state), ["1"])
        self.assertAlmostEqual(state["1"]["since"], now - 10 * HOUR, delta=5)   # countdown kept
        self.assertIn("inactive for 49 h", self.main_log())

    def test_first_sighting_starts_the_countdown(self):
        self.server.add("1", "New", 60 * MIN_MS)
        self.run_script()
        self.assertEqual(self.server.scrobbled, [])
        self.assertAlmostEqual(self.read_state()["1"]["since"], time.time(), delta=5)

    def test_position_change_restarts_the_countdown(self):
        self.server.add("1", "Resumed", 60 * MIN_MS)
        self.write_state({"1": {"offset": 55 * MIN_MS, "since": time.time() - 60 * HOUR}})
        self.run_script()
        self.assertEqual(self.server.scrobbled, [])
        self.assertAlmostEqual(self.read_state()["1"]["since"], time.time(), delta=5)

    def test_playing_or_paused_items_are_never_touched(self):
        self.server.add("1", "Short but playing", 20_000)
        self.server.add("2", "Expired but playing", 70 * MIN_MS)
        self.server.playing = {"1", "2"}
        self.write_state({"2": {"offset": 70 * MIN_MS, "since": time.time() - 100 * HOUR}})
        self.run_script()
        self.assertEqual(self.server.scrobbled, [])
        for key in ("1", "2"):
            self.assertAlmostEqual(self.read_state()[key]["since"], time.time(), delta=5)
        # once playback stops, the short one is handled
        self.server.playing = set()
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["1"])

    def test_unwatched_and_no_resume_position_are_ignored(self):
        self.server.add("1", "Never watched", 90_000, views=0)
        self.server.add("2", "Watched, no resume", 0, views=2)
        self.run_script()
        self.assertEqual(self.server.scrobbled, [])
        self.assertEqual(self.read_state(), {})

    def test_state_from_a_previous_run_is_reused(self):
        self.server.add("1", "Movie", 50 * MIN_MS)
        self.run_script()
        first = self.read_state()["1"]["since"]
        time.sleep(0.05)
        self.run_script()
        self.assertEqual(self.read_state()["1"]["since"], first)


class ConfigFile(Base):
    def test_behavior_settings_are_used(self):
        self.server.add("1", "Ten minutes", 10 * MIN_MS)       # long by default, short at 20 min
        self.server.add("2", "Two hours old", 40 * MIN_MS)     # waiting 2 h, delay is 1 h
        self.server.add("3", "Fifty minutes", 50 * MIN_MS)     # first seen: keeps waiting
        self.set("behavior", "short_play_minutes", 20)
        self.set("behavior", "delay_hours", 1)
        self.write_state({"2": {"offset": 40 * MIN_MS, "since": time.time() - 2 * HOUR}})
        self.run_script()
        self.assertEqual(sorted(self.server.scrobbled), ["1", "2"])
        self.assertEqual(list(self.read_state()), ["3"])

    def test_decimal_values_are_accepted(self):
        self.server.add("1", "Two minutes", 2 * MIN_MS)
        self.server.add("2", "One minute", 1 * MIN_MS)
        self.set("behavior", "short_play_minutes", 1.5)
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["2"])

    def test_episodes_are_handled_only_when_enabled(self):
        self.server.episodes.append(("E1", "Episode", 1, 30_000))
        self.run_script()
        self.assertEqual(self.server.scrobbled, [])
        self.set("behavior", "include_episodes", "yes")
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["E1"])

    def test_environment_overrides_the_config_file(self):
        self.set("plex", "token", "FILE-TOKEN")
        os.environ["PLEX_TOKEN"] = "ENV-TOKEN"
        self.run_script()
        self.assertEqual(self.server.tokens_seen, {"ENV-TOKEN"})

    def test_environment_can_provide_the_server_address(self):
        self.set("plex", "url", f"http://127.0.0.1:{free_port()}")   # wrong on purpose
        os.environ["PLEX_URL"] = self.server.url
        code, _ = self.run_script()
        self.assertEqual(code, 0)

    def test_special_characters_in_values(self):
        self.set("plex", "token", "abc%def#x y")
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.tokens_seen, {"abc%def#x y"})

    def test_no_config_file_needed_when_the_environment_has_everything(self):
        self.m.DEFAULT_CONFIG_PATH = os.path.join(self.dir, "does-not-exist.ini")
        os.environ.update(PLEX_URL=self.server.url, PLEX_TOKEN="ENV-TOKEN",
                          KEEP_WATCHED_DATA_DIR=self.dir)
        self.server.add("1", "Short", 30_000)
        code, _ = self.run_raw([])
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, ["1"])
        self.assertIn("MARKED AS WATCHED", self.main_log())

    def test_config_path_from_the_environment(self):
        self.write_config()
        os.environ["KEEP_WATCHED_CONFIG"] = self.config_path
        self.m.DEFAULT_CONFIG_PATH = os.path.join(self.dir, "does-not-exist.ini")
        self.server.add("1", "Short", 30_000)
        code, _ = self.run_raw([])
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, ["1"])

    def test_empty_values_mean_default(self):
        self.set("behavior", "short_play_minutes", "")
        self.set("plex", "url", "")
        os.environ["PLEX_URL"] = self.server.url
        self.server.add("1", "Ten minutes", 10 * MIN_MS)
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.m.SHORT_PLAY_MINUTES, 5)
        self.assertEqual(self.server.scrobbled, [])

    def test_missing_explicit_config_file(self):
        code, _ = self.run_script(["--config", os.path.join(self.dir, "missing.ini")],
                                  write_config=False)
        self.assertEqual(code, 1)
        self.assertIn("config file not found", self.stderr)

    def test_malformed_config_file(self):
        with open(self.config_path, "w") as f:
            f.write("this is not an ini file\n")
        code, _ = self.run_script(write_config=False)
        self.assertEqual(code, 1)
        self.assertIn("cannot read config file", self.stderr)

    def test_config_errors_are_printed_but_no_log_file_is_created_anywhere(self):
        self.set("behavior", "delay_hours", "abc")
        self.set("files", "data_dir", os.path.join(self.dir, "elsewhere"))
        code, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("ERROR: invalid number", self.stderr)
        self.assertEqual(sorted(os.listdir(self.dir)), ["config.ini"])   # nothing but the config

    def test_unknown_setting_is_rejected(self):
        self.set("behavior", "delay_hour", 3)              # typo
        code, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("unknown setting [behavior] delay_hour", self.stderr)
        self.assertEqual(self.server.requests, 0)

    def test_invalid_values_are_rejected_with_a_clear_message(self):
        cases = [
            ("behavior", "delay_hours", "abc", "invalid number for [behavior] delay_hours"),
            ("behavior", "short_play_minutes", "-1", "[behavior] short_play_minutes must not be negative"),
            ("behavior", "include_episodes", "maybe", "invalid yes/no value for [behavior] include_episodes"),
            ("logging", "max_lines", "0", "[logging] max_lines must be at least 1"),
            ("logging", "max_lines", "ten", "invalid whole number for [logging] max_lines"),
        ]
        for section, key, value, expected in cases:
            with self.subTest(setting=f"{section}.{key}={value}"):
                self.cfg["behavior"] = {}
                self.cfg["logging"] = {}
                self.set(section, key, value)
                code, _ = self.run_script()
                self.assertEqual(code, 1)
                self.assertIn(expected, self.stderr)
                self.assertEqual(self.server.requests, 0)

    def test_example_config_is_valid_and_complete(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(EXAMPLE_CONFIG, encoding="utf-8")
        in_example = {(s, k) for s in parser.sections() for k in parser[s]}
        self.assertEqual(in_example, set(self.m.SETTINGS))        # no setting missing or invented
        self.m.read_config(EXAMPLE_CONFIG)                          # parses and validates
        self.m.load_settings(EXAMPLE_CONFIG)
        defaults = self.m.default_values()
        for setting in (("behavior", "short_play_minutes"), ("behavior", "delay_hours"),
                        ("behavior", "include_episodes"), ("logging", "basic_log"),
                        ("logging", "verbose"), ("logging", "max_lines"),
                        ("logging", "verbose_max_lines")):
            self.assertEqual(self.m.read_config(EXAMPLE_CONFIG)[setting], defaults[setting], setting)
        self.assertEqual(self.m.PLEX_TOKEN, "YOUR_PLEX_TOKEN")       # placeholder that main() rejects


class RealCommandLine(Base):
    """Runs the script as a separate process, the way cron does."""

    def test_default_config_is_config_ini_next_to_the_script(self):
        shutil.copy(SCRIPT, self.dir)
        self.cfg["files"] = {}                       # logs and state next to the script too
        self.write_config()                          # writes <dir>/config.ini
        self.server.add("1", "Short", 30_000)
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, elsewhere, True)
        env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
        proc = subprocess.run([sys.executable, os.path.join(self.dir, "plex_keep_watched.py")],
                              cwd=elsewhere, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.scrobbled, ["1"])
        self.assertIn("MARKED AS WATCHED", self.main_log())
        self.assertEqual(os.listdir(elsewhere), [])  # nothing written in the working directory

    def test_missing_config_gives_an_actionable_error(self):
        shutil.copy(SCRIPT, self.dir)
        env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
        proc = subprocess.run([sys.executable, os.path.join(self.dir, "plex_keep_watched.py")],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Plex token is not set", proc.stderr)
        self.assertIn("config.ini", proc.stderr)


class Errors(Base):
    def test_placeholder_or_missing_token_is_reported_without_touching_the_network(self):
        for token in ("YOUR_PLEX_TOKEN", ""):
            with self.subTest(token=token):
                self.set("plex", "token", token)
                code, _ = self.run_script()
                self.assertEqual(code, 1)
                self.assertIn("ERROR: Plex token is not set", self.main_log())
                self.assertEqual(self.server.requests, 0)

    def test_errors_go_to_stderr_not_stdout(self):
        self.set("plex", "token", "")
        code, printed = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("ERROR", self.stderr)
        self.assertNotIn("ERROR", printed)

    def test_connection_error_gives_exit_code_1_and_a_log_line(self):
        self.set("plex", "url", f"http://127.0.0.1:{free_port()}")
        code, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", self.main_log())
        self.assertIn("ERROR:", self.stderr)

    def test_non_ascii_titles_survive_an_ascii_only_terminal(self):
        self.server.add("1", "Amélie", 30_000)
        self.write_config()
        fake_stdout = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        with contextlib.redirect_stdout(fake_stdout):
            code = self.m.cli(["--config", self.config_path])
        self.assertEqual(code, 0)
        self.assertIn("Amélie", self.main_log())     # the log file keeps the real title


class Logging(Base):
    def test_main_log_keeps_the_last_100_lines_by_default(self):
        self.run_script()                              # applies the settings
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(130):
                self.m.log(f"entry {i}")
        lines = self.main_log().splitlines()
        self.assertEqual(len(lines), 100)
        self.assertTrue(lines[0].endswith("entry 30"))
        self.assertTrue(lines[-1].endswith("entry 129"))

    def test_max_lines_setting(self):
        self.set("logging", "max_lines", 10)
        self.run_script()
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(25):
                self.m.log(f"entry {i}")
        self.assertEqual(len(self.main_log().splitlines()), 10)

    def test_normal_mode_writes_no_verbose_file(self):
        self.server.add("1", "Short", 30_000)
        self.server.add("2", "Long", 50 * MIN_MS)
        self.run_script()
        self.assertIsNone(self.verbose_log())
        self.assertNotIn("WAITING", self.main_log())

    def test_verbose_mode_describes_every_decision(self):
        self.server.add("1", "Short", 30_000)
        self.server.add("2", "Playing", 20_000)
        self.server.add("3", "Waiting", 50 * MIN_MS)
        self.server.add("4", "New", 60 * MIN_MS)
        self.server.add("5", "Expired", 40 * MIN_MS)
        self.server.add("6", "Never watched", 90_000, views=0)
        self.server.playing = {"2"}
        now = time.time()
        self.write_state({
            "3": {"offset": 50 * MIN_MS, "since": now - 10 * HOUR},
            "5": {"offset": 40 * MIN_MS, "since": now - 49 * HOUR},
        })
        self.set("logging", "verbose", "true")
        code, printed = self.run_script()
        v = self.verbose_log()
        self.assertEqual(code, 0)
        self.assertIn("=== RUN", v)
        self.assertIn("short-play threshold 5 min, delay 48 h", v)
        self.assertIn("Tautulli disabled", v)
        self.assertIn("1 playback(s) in progress on Plex", v)
        self.assertIn("SKIPPED  'Playing' (id 2)", v)
        self.assertIn("WAITING  'Waiting' (id 3)", v)
        self.assertIn("position unchanged since the previous run", v)
        self.assertTrue("(in 38 h" in v or "(in 37 h 59" in v)       # 48 h - 10 h
        self.assertIn("WAITING  'New' (id 4)", v)
        self.assertIn("first seen, countdown started", v)
        self.assertIn("MARKED AS WATCHED (short play)", v)
        self.assertIn("MARKED AS WATCHED (inactive for 49 h)", v)
        self.assertNotIn("Never watched", v)
        self.assertIn("Summary: 6 item(s) scanned, 5 already watched with a resume position "
                      "-> 2 marked watched, 2 waiting, 1 skipped", v)
        # the main log stays limited to the changes
        main = self.main_log()
        self.assertIn("MARKED AS WATCHED", main)
        self.assertNotIn("WAITING", main)
        self.assertNotIn("SKIPPED", main)
        # everything is also printed (handy for a manual run)
        self.assertIn("WAITING  'Waiting'", printed)

    def test_verbose_flag_on_the_command_line(self):
        self.server.add("1", "Long", 50 * MIN_MS)
        _, printed = self.run_script(["-v"])
        self.assertIn("WAITING  'Long'", printed)
        self.assertIn("WAITING  'Long'", self.verbose_log())

    def test_verbose_log_is_capped(self):
        self.set("logging", "verbose", "true")
        self.set("logging", "verbose_max_lines", 10)
        self.server.add("1", "Long", 50 * MIN_MS)
        for _ in range(5):
            self.run_script()
        self.assertEqual(len(self.verbose_log().splitlines()), 10)

    def test_errors_show_up_in_both_logs_when_verbose(self):
        self.set("logging", "verbose", "true")
        self.set("plex", "token", "")
        self.run_script()
        self.assertIn("ERROR", self.main_log())
        self.assertIn("ERROR", self.verbose_log())

    def test_data_dir_setting_creates_the_folder(self):
        target = os.path.join(self.dir, "sub", "deeper")
        self.set("files", "data_dir", target)
        self.server.add("1", "Short", 30_000)
        self.run_script()
        self.assertIn("MARKED AS WATCHED", self.read(os.path.join(target, "keep_watched.log")))
        self.assertTrue(os.path.exists(os.path.join(target, "keep_watched_state.json")))

    def test_data_dir_environment_variable_wins(self):
        target = os.path.join(self.dir, "from-env")
        os.environ["KEEP_WATCHED_DATA_DIR"] = target
        self.server.add("1", "Short", 30_000)
        self.run_script()
        self.assertIn("MARKED AS WATCHED", self.read(os.path.join(target, "keep_watched.log")))
        self.assertIsNone(self.read(self.log_file))


class UnwritableLog(Base):
    def test_a_log_file_that_cannot_be_written_does_not_stop_the_job(self):
        os.mkdir(self.log_file)                       # a directory where the log file should be
        self.server.add("1", "Short", 30_000)
        self.server.add("2", "Short too", 40_000)
        self.server.add("3", "Long", 50 * MIN_MS)
        code, printed = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, ["1", "2"])           # the job is done
        self.assertIn("3", self.read_state())                         # state is saved
        self.assertEqual(self.stderr.count("WARNING: cannot write the log file"), 1)  # reported once
        self.assertIn("MARKED AS WATCHED", printed)

    def test_unwritable_data_dir_reports_the_real_error(self):
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w") as f:
            f.write("a file, not a folder")
        self.set("files", "data_dir", os.path.join(blocker, "data"))
        self.server.add("1", "Short", 30_000)
        code, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", self.stderr)                          # a message, not a traceback
        self.assertNotIn("Traceback", self.stderr)


class BasicLogCanBeDisabled(Base):
    def setUp(self):
        super().setUp()
        self.set("logging", "basic_log", "false")

    def test_no_basic_log_file_but_everything_else_works(self):
        self.server.add("1", "Short", 30_000)
        self.server.add("2", "Long", 50 * MIN_MS)
        code, printed = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, ["1"])               # the job is still done
        self.assertIsNone(self.read(self.log_file))                  # but no log file
        self.assertIn("2", self.read_state())                        # state is still kept (required)
        self.assertIn("MARKED AS WATCHED (short play)", printed)     # and still printed

    def test_errors_are_still_reported_on_stderr_and_exit_code(self):
        self.set("plex", "token", "")
        code, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIsNone(self.read(self.log_file))
        self.assertIn("ERROR: Plex token is not set", self.stderr)

    def test_warnings_go_to_stderr(self):
        self.set("tautulli", "url", self.server.url)
        self.set("tautulli", "api_key", "WRONG")
        self.server.add("1", "Short", 2 * MIN_MS)
        code, printed = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("WARNING: Tautulli unusable", self.stderr)
        self.assertNotIn("WARNING", printed)
        self.assertIsNone(self.read(self.log_file))

    def test_verbose_log_still_works_on_its_own(self):
        self.set("logging", "verbose", "true")
        self.server.add("1", "Short", 30_000)
        self.server.add("2", "Long", 50 * MIN_MS)
        self.run_script()
        v = self.verbose_log()
        self.assertIn("MARKED AS WATCHED (short play)", v)           # changes are in there too
        self.assertIn("WAITING  'Long'", v)
        self.assertIsNone(self.read(self.log_file))


class DryRun(Base):
    def test_dry_run_changes_and_writes_nothing(self):
        self.server.add("1", "Short", 30_000)
        self.server.add("2", "Expired", 40 * MIN_MS)
        self.server.add("3", "Waiting", 50 * MIN_MS)
        self.write_state({
            "2": {"offset": 40 * MIN_MS, "since": time.time() - 49 * HOUR},
            "3": {"offset": 50 * MIN_MS, "since": time.time() - 5 * HOUR},
        })
        state_before = self.read(self.state_file)
        code, printed = self.run_script(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, [])                  # Plex untouched
        self.assertEqual(self.read(self.state_file), state_before)   # state untouched
        self.assertIsNone(self.read(self.log_file))                  # no log files
        self.assertIsNone(self.verbose_log())
        self.assertIn("[DRY RUN] WOULD MARK AS WATCHED (short play): 'Short'", printed)
        self.assertIn("[DRY RUN] WOULD MARK AS WATCHED (inactive for 49 h): 'Expired'", printed)
        self.assertIn("WAITING  'Waiting'", printed)
        self.assertIn("2 would be marked watched, 1 waiting", printed)
        self.assertIn("dry run: nothing will be changed", printed)

    def test_dry_run_does_not_stick_to_the_next_run(self):
        self.server.add("1", "Short", 30_000)
        self.run_script(["--dry-run"])
        self.assertEqual(self.server.scrobbled, [])
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["1"])


class TautulliOptional(Base):
    def enable_tautulli(self):
        self.set("tautulli", "url", self.server.url + "/")
        self.set("tautulli", "api_key", "SECRET")

    def test_disabled_without_credentials(self):
        self.server.add("A", "Jump to 42 min", 42 * MIN_MS)
        self.server.history["A"] = [row(1, 30)]
        for url, key in (("", ""), (self.server.url, ""), ("", "SECRET"), ("  ", "  ")):
            with self.subTest(url=url, key=key):
                self.set("tautulli", "url", url)
                self.set("tautulli", "api_key", key)
                self.run_script()
                self.assertFalse(self.m.tautulli_enabled())
                self.assertEqual(self.server.api_calls, [])          # no Tautulli call at all
                self.assertEqual(self.server.scrobbled, [])          # 42 min position -> real watch

    def test_real_play_time_beats_the_position(self):
        self.enable_tautulli()
        self.server.add("A", "Jump to 42 min, 30 s played", 42 * MIN_MS)
        self.server.history["A"] = [row(1, 30)]
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["A"])
        self.assertIn("short play, 0:30 actually played", self.main_log())
        call = self.server.api_calls[0]
        self.assertEqual(call["cmd"], ["get_history"])
        self.assertEqual(call["grouping"], ["0"])
        self.assertEqual(call["rating_key"], ["A"])

    def test_resumed_viewing_adds_up_the_sessions(self):
        self.enable_tautulli()
        self.server.add("B", "Resumed for 10 s", 40 * MIN_MS + 10_000)
        self.server.history["B"] = [row(1, 10), row(3, 2400)]        # 2410 s in total
        self.run_script()
        self.assertEqual(self.server.scrobbled, [])

    def test_old_viewing_outside_the_chain_is_ignored(self):
        self.enable_tautulli()
        self.server.add("C", "Old viewing", 40 * MIN_MS)
        self.server.history["C"] = [row(1, 20), row(200, 2400)]      # gap > 48 h
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["C"])

    def test_completed_viewing_stops_the_chain(self):
        self.enable_tautulli()
        self.server.add("D", "Rewatched", 30 * MIN_MS)
        self.server.history["D"] = [row(1, 20), row(5, 7000, watched=1)]
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["D"])               # only 20 s of the new viewing

    def test_no_usable_history_falls_back_to_the_position(self):
        self.enable_tautulli()
        self.server.add("E", "No history, 10 min", 10 * MIN_MS)
        self.server.add("F", "No history, 2 min", 2 * MIN_MS)
        self.server.add("G", "Only a completed viewing", 30 * MIN_MS)
        self.server.history["G"] = [row(5, 7000, watched=1)]
        self.set("logging", "verbose", "true")
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["F"])
        self.assertIn("Tautulli: no usable history", self.verbose_log())

    def test_nested_response_envelope_is_understood(self):
        self.enable_tautulli()
        self.server.add("N1", "Nested envelope", 42 * MIN_MS)
        self.server.history["N1"] = [row(1, 30)]
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["N1"])

    def test_tautulli_error_warns_once_and_falls_back(self):
        self.enable_tautulli()
        self.server.add("1", "First", 30 * MIN_MS)
        self.server.add("2", "Second", 2 * MIN_MS)
        self.server.add("3", "Third", 3 * MIN_MS)
        self.server.history_errors = {"1"}
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["2", "3"])           # position rule
        log = self.main_log()
        self.assertEqual(log.count("WARNING"), 1)
        self.assertIn("Tautulli unusable (boom)", log)
        self.assertIn("WARNING", self.stderr)
        self.assertEqual(len(self.server.api_calls), 1)               # no retry for the others
        self.assertNotIn("SECRET", log)

    def test_wrong_api_key_does_not_crash(self):
        self.set("tautulli", "url", self.server.url)
        self.set("tautulli", "api_key", "WRONG")
        self.server.add("1", "Short", 2 * MIN_MS)
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, ["1"])
        self.assertIn("Invalid apikey", self.main_log())

    def test_unreachable_tautulli_does_not_crash(self):
        self.set("tautulli", "url", f"http://127.0.0.1:{free_port()}")
        self.set("tautulli", "api_key", "SECRET")
        self.server.add("1", "Short", 2 * MIN_MS)
        code, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.scrobbled, ["1"])
        self.assertEqual(self.main_log().count("WARNING"), 1)

    def test_tautulli_settings_from_the_environment(self):
        os.environ["TAUTULLI_URL"] = self.server.url
        os.environ["TAUTULLI_API_KEY"] = "SECRET"
        self.server.add("A", "Jump to 42 min", 42 * MIN_MS)
        self.server.history["A"] = [row(1, 30)]
        self.run_script()
        self.assertEqual(self.server.scrobbled, ["A"])


if __name__ == "__main__":
    unittest.main()
