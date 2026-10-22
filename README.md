# plex-keep-watched

Keep your already-watched Plex movies marked as **watched**, even if you start them for a few seconds.

## The problem

You have already seen a movie. You launch it again for a moment (misclick, quick check of a scene, wrong film), stop it, and Plex stores a resume position. The movie loses its "watched" check mark and shows up as **in progress**, and it can reappear in your *Continue Watching* row.

Plex has no setting to prevent that. This script fixes it after the fact: it runs regularly, finds already-watched movies that have turned back into "in progress", and marks them as watched again, **but only when it is safe to do so**, so you can still pause and resume a movie you are really rewatching.

## How it decides

On every run, for each movie that was watched before and now has a resume position:

| Situation | What happens |
|---|---|
| It is playing or paused in a player right now | Left alone. |
| **Short play**: you watched less than `short_play_minutes` (default 5) | Marked as watched again on this run. |
| **Real watch**: more than that | Marked as watched again only after the position has not moved for `delay_hours` (default 48). If you resume it, the countdown restarts. |

What "short" means depends on whether you use [Tautulli](https://tautulli.com/) (optional):

- **Without Tautulli**, the script only sees the resume position. A resume position under 5 minutes is a short play.
- **With Tautulli**, the script uses the time you actually played. "I jumped to minute 42 and watched 30 seconds" is then correctly treated as a short play instead of a real watch. If Tautulli has no usable history for a movie, or does not answer, the script falls back to the resume position for that movie.

## Requirements

- Python 3.8 or newer. No third-party packages (standard library only).
- A Plex Media Server you can reach over the network, and your Plex token.
- Optional: Tautulli.
- Something that runs the script regularly: cron, a TrueNAS cron job, Windows Task Scheduler, a systemd timer...

Developed and used on TrueNAS Scale, with Plex and Tautulli installed as apps.

## Quick start

**1. Get the files.** Download `plex_keep_watched.py` and `config.example.ini` into the same folder (for example `/opt/plex-keep-watched/`).

**2. Create your config file** by copying the example, and keep it private (it contains your token):

```sh
cp config.example.ini config.ini
chmod 600 config.ini
```

**3. Edit `config.ini`.** At minimum, set your server address and token in the `[plex]` section. Every setting is explained by a comment right above it.

To get your Plex token: in Plex Web, open any item, open the "..." menu, choose *Get Info*, then *View XML*. The token is the `X-Plex-Token=` value in the URL of that page (see Plex's support article "Finding an authentication token / X-Plex-Token" if the menus differ on your version). Treat it like a password.

**4. Try it without changing anything:**

```sh
python3 plex_keep_watched.py --dry-run
```

It prints what it *would* do and changes nothing: no Plex change, no state file, no log file.

**5. Schedule it**, for example every 30 minutes:

```cron
*/30 * * * * /usr/bin/python3 /opt/plex-keep-watched/plex_keep_watched.py > /dev/null
```

The script finds `config.ini` next to itself, whatever the working directory. It writes its own logs (see below), so discarding its console output is fine. Warnings and errors go to stderr, which cron will mail to you if mail is set up; add `2>&1` after `> /dev/null` if you prefer to silence those too.

<details>
<summary>TrueNAS Scale</summary>

*System Settings > Advanced > Cron Jobs > Add*:

- **Command**: `python3 /mnt/pool/path/to/plex_keep_watched.py` (escape or quote spaces in the path)
- **Run as user**: a user that can read `config.ini` and write to the script's folder (the script creates its state and log files there), or set `data_dir` in the config
- **Schedule**: every 30 minutes
- The command runs on the TrueNAS host, which already has Python 3. Use the TrueNAS IP and the port Plex is exposed on for the Plex `url`.
</details>

Run it once by hand before relying on the schedule. If something is wrong you will see an `ERROR:` line.

## Configuration

Settings live in `config.ini` (an `.ini` file, standard library only). Look at [`config.example.ini`](config.example.ini): it lists every setting with its explanation.

| Section | Setting | Default | Meaning |
|---|---|---|---|
| `[plex]` | `url` | `http://127.0.0.1:32400` | Address of your Plex server |
| `[plex]` | `token` | *(required)* | Your Plex token |
| `[behavior]` | `short_play_minutes` | `5` | Below this, a play is "short" and reset on the next run |
| `[behavior]` | `delay_hours` | `48` | Waiting time without activity before a real watch is reset |
| `[behavior]` | `include_episodes` | `false` | Also handle TV episodes, not only movies |
| `[tautulli]` | `url` | *(empty)* | Tautulli address, e.g. `http://192.168.1.10:8181` |
| `[tautulli]` | `api_key` | *(empty)* | Tautulli API key |
| `[logging]` | `basic_log` | `true` | Write `keep_watched.log` (set `false` to turn it off) |
| `[logging]` | `verbose` | `false` | Detailed run log in `keep_watched_verbose.log` |
| `[logging]` | `max_lines` | `100` | Lines kept in the basic log |
| `[logging]` | `verbose_max_lines` | `2000` | Lines kept in the verbose log |
| `[files]` | `data_dir` | *(script folder)* | Where the state and log files are written |

Notes:
- An empty value means "use the default". Put comments on their own line, not after a value.
- A typo in a setting name, or an invalid value, stops the script with a clear message instead of being ignored.

**Where the script looks for the config**, in this order: `--config FILE`, the `KEEP_WATCHED_CONFIG` environment variable, then `config.ini` next to the script.

**Environment variables** override the config file, which is handy for containers or when you do not want a token in a file: `PLEX_URL`, `PLEX_TOKEN`, `TAUTULLI_URL`, `TAUTULLI_API_KEY`, `KEEP_WATCHED_DATA_DIR`. With all of `PLEX_URL`, `PLEX_TOKEN` (and optionally the others) set, no config file is needed at all.

**Precedence**: command-line options, then environment variables, then the config file, then the built-in defaults.

**Command-line options**: `--config FILE` / `-c`, `--dry-run` / `-n` (change nothing), `--verbose` / `-v` (detailed log for this run).

## Using Tautulli (optional)

Fill in **both** `url` and `api_key` in the `[tautulli]` section (Tautulli > Settings > Web Interface > API Key). If either is empty, Tautulli is not used at all.

> **Important: change one Tautulli setting first.**
> By default, Tautulli does **not** record plays shorter than 2 minutes. Short plays, the very thing this script is about, would then be invisible to it. In Tautulli, go to *Settings > General* and set **Logging Ignore Interval** (the minimum duration before a play is recorded, 120 seconds by default) to **1 second**. The label may differ slightly between Tautulli versions.

When Tautulli is enabled, the script adds up the play time of the current viewing (consecutive plays, stopping at a completed viewing or at a gap longer than `delay_hours`), so a quick peek at the end of a movie you were really watching still counts as part of a real watch. If Tautulli cannot be reached or rejects the request, the script logs one `WARNING` line and uses the resume position instead; it does not fail.

## Logs

Files are written to the data directory (next to the script unless you set `data_dir`):

- `keep_watched.log`: the last 100 changes, plus warnings and errors. **Optional**: set `basic_log = false` if you do not want it.
- `keep_watched_state.json`: the countdowns of the movies that are waiting. The script needs it to work (it is not a log), and it is safe to delete (countdowns restart).
- `keep_watched_verbose.log`: only with verbose mode (last 2000 lines).

```
2026-10-22 09:30:02  MARKED AS WATCHED (short play): 'Some Movie' (id 1234) - stopped at 0:30
2026-10-22 10:00:02  MARKED AS WATCHED (short play, 0:30 actually played): 'Other Movie' (id 5678) - stopped at 42:00
2026-10-24 14:00:03  MARKED AS WATCHED (inactive for 48 h): 'Third Movie' (id 91011) - stopped at 61:12
```

**Turning the basic log off** (`basic_log = false`) only stops the file. The script still works the same way, still prints what it does on the console, and warnings and errors are still printed on stderr, with a non-zero exit code on failure, so a scheduler can report a broken run. The verbose log, if enabled, also contains the changes, so you can have it without the basic log.

Two details about failures:
- **Problems in `config.ini` itself** (missing file, typo, invalid value) are only printed on stderr, never written to a log file: until the config is read, the script does not know where your logs should go.
- **A log file that cannot be written** (permissions, full disk) does not stop the job. You get one `WARNING: cannot write the log file ...` line on stderr per run, and the script carries on.

### Verbose mode

Set `verbose = true`, or run once with `--verbose`, to see every decision, including what is **waiting** and until when, and what is skipped and why:

```
=== RUN: short-play threshold 5 min, delay 48 h, Tautulli disabled, 1 playback(s) in progress on Plex
MARKED AS WATCHED (short play): 'Film A' (id 1) - stopped at 0:30
SKIPPED  'Film B' (id 2) - position 0:20: playing or paused, countdown reset
WAITING  'Film C' (id 4) - position 50:00: real watch (based on resume position 50:00 >= 5 min); position unchanged since the previous run; will be marked watched from 2026-10-23 23:34 (in 37 h 59 min)
Summary: 7 item(s) scanned, 5 already watched with a resume position -> 2 marked watched, 2 waiting, 1 skipped (playing now)
```

Movies that were never watched, or are fully watched with no resume position, are never candidates, so they do not appear line by line (only in the "scanned" count).

## Good to know

- **Per Plex user.** "Watched" status is stored per user. The script acts for the account that owns the token; other users of your server (including managed/home users) are not handled.
- **A genuine re-watch that you abandon halfway will also be reset**, after the delay, because the script cannot tell it from an accidental play. That is the trade-off of this approach.
- **The "real watch" test uses the position**, not the time watched, unless Tautulli is enabled. Without Tautulli, jumping to minute 42 and stopping counts as a real watch (reset after `delay_hours`).
- **It uses the Plex HTTP API** (`/:/scrobble` to mark an item as watched). Depending on your Plex version, marking an item as watched this way may also update its view count or last-viewed date.
- **Resets happen on a run**, so the actual delay is the setting plus up to one scheduling interval.
- **Anything playing right now is skipped.** A paused session counts as playing.
- **Keep `config.ini` private.** It holds your token. It is in `.gitignore`; share `config.example.ini`, never your own `config.ini`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ERROR: Plex token is not set` | `config.ini` is missing, still has the placeholder token, or sits somewhere the script does not look. Set `[plex] token`, or the `PLEX_TOKEN` variable. |
| `ERROR: config file not found` | The path given with `--config` or `KEEP_WATCHED_CONFIG` is wrong. |
| `ERROR: unknown setting [section] name` | A typo in `config.ini`. Compare with `config.example.ini`. |
| `ERROR: invalid ... value for [section] name` | A value of the wrong kind (text where a number or `true`/`false` is expected). |
| `ERROR: <urlopen error ... Connection refused>` | Wrong Plex `url` or port. If Plex runs as a container/app, use the host IP and the port it is exposed on. |
| `ERROR: HTTP Error 401: Unauthorized` | Invalid token. |
| Cron job reports exit status 1 | Run the command by hand (without `> /dev/null`), or read `keep_watched.log` if the basic log is on. |
| Nothing is ever marked | Run with `--dry-run --verbose` to see what the script thinks of each movie. |
| `WARNING: Tautulli unusable (...)` | Wrong URL or API key, or Tautulli is down. The script keeps working with the resume position. |
| Short plays are not detected with Tautulli | Set Tautulli's *Logging Ignore Interval* to 1 second (see above). |
| Permission error writing the log or state | The user running the script cannot write to the data directory: fix the permissions or set `data_dir`. |

## Tests

The tests use fake Plex and Tautulli servers on localhost, so no real server is needed:

```sh
python3 -m unittest discover -s tests -v
```

## Disclaimer

Unofficial tool, not affiliated with or endorsed by Plex, Inc. or Tautulli. It changes the watched state of items in your library through the Plex API: try `--dry-run` first, and use it at your own risk.

## License

[MIT](LICENSE)
