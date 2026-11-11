#!/usr/bin/env python3
"""Read-only status helper for the io.github.manish0467.btrfs-snapshots widget.

Architecture (docs/DESIGN.md):
  * The panel (QML) runs this script with `check` and reads ONE JSON document
    from stdout. Everything the bar and panel display is derived here, so all
    parsing, classification and formatting logic is testable outside the shell.
  * Data comes from two tiers:
      A. /boot/limine.conf (world-readable) - the snapshot view that
         limine-snapper-sync maintains: snapshot numbers (in the kernel
         command line), dates (entry names) and descriptions (comments).
      B. `snapper --jsonout -c <cfg> list` - only works when the user has been
         granted access in the Snapper config; probed lazily, result cached.
    Plus: `snapper list-configs`, /etc/snapper/configs/* (retention),
    os.statvfs() (filesystem usage) and /etc/limine-snapper-sync.conf (ESP cap).
  * PRIVACY: limine.conf contains machine-ids, UUIDs and kernel command lines.
    Only snapshot number, date and description are ever extracted; every other
    byte is discarded and never logged or stored. state.json holds only
    statuses and timestamps. Fixtures shipped with the plugin are synthetic.
  * PRIVILEGES: this helper runs as the plain user. It spawns exactly one
    child process kind - `snapper` in list mode with a fixed argv shape - and
    never elevates privileges. It never deletes, restores or cleans up; it
    only ever reads. See README.md for the full privilege statement.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
VERSION = "0.1.0"
PLUGIN_ID = "io.github.manish0467.btrfs-snapshots"

# ---------------------------------------------------------------------------
# Defaults shared with Panel.qml (mirrored there in the settings JSON string)
# ---------------------------------------------------------------------------
DEFAULT_STALE_DAYS = 7          # 0 disables the stale state entirely
DEFAULT_DISK_WARN_PERCENT = 90  # root filesystem usage that turns the widget lowdisk
DEFAULT_ESP_WARN_PERCENT = 85   # used only when limine-snapper-sync.conf is unreadable
MAX_POLL_MINUTES_FLOOR = 10     # not used by the helper; QML clamps

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------
MAX_LIMINE_BYTES = 2 * 1024 * 1024     # limine.conf is a few KiB; anything bigger
                                       # is treated as unparsable, never streamed
SNAPPER_TIMEOUT = 5.0                  # seconds; the D-Bus deny answer is instant
REAP_TIMEOUT = 2.0                     # grace period when reaping a timed-out child
TIER_B_PROBE_INTERVAL = 3600.0         # cache "denied" for an hour between probes
MAX_ERRORS = 8                         # keep the JSON small even on bad days
MAX_DESC_LEN = 200                     # snapshot descriptions are untrusted text

# The one and only creation command (constant; see docs/DESIGN.md D2). The
# helper only echoes it for the confirmation dialog - it never runs it.
CREATION_COMMAND = "omarchy-launch-floating-terminal-with-presentation omarchy-snapshot create"

MOCK_DIR = Path(__file__).resolve().parent.parent / "mock"

LIMINE_CONF_CANDIDATES = ("/boot/limine.conf", "/efi/limine.conf",
                          "/boot/efi/limine.conf", "/limine/limine.conf")

CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SNAP_NUM_RE = re.compile(r"\.snapshots/(\d+)/snapshot")
LIMITE_ENTRY_RE = re.compile(r"^\s*///([^/].*)$")     # `///date`, not `////kernel`
LIMITE_KERNEL_RE = re.compile(r"^\s*////")            # kernel entries under a snapshot
LIMITE_SECTION_RE = re.compile(r"^\s*//Snapshots\s*$")
LIMITE_HEADER_COUNT_RE = re.compile(r"^\s*comment:\s*\d+\s*/\s*\d+\s+snapshots?\s*$")
LIMITE_COMMENT_RE = re.compile(r"^\s*comment:\s*(.*)$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$")


# ---------------------------------------------------------------------------
# Small helpers: JSON I/O, subprocess, text sanitisation
# ---------------------------------------------------------------------------
def read_json(path, default):
    """Read JSON, returning `default` for any error. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    """Write JSON via a temp file + rename, so a crash never truncates state."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(str(tmp), str(path))
    except OSError:
        pass  # state is an optimisation; losing it only costs a re-probe


def clip(text, limit=MAX_DESC_LEN):
    """Sanitise untrusted text for JSON transport: str, control characters
    (NUL, tabs, newlines, ...) collapsed to spaces, stripped, truncated."""
    if text is None:
        return None
    text = "".join(" " if c < " " else c for c in str(text))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or None


def run_cmd(argv, timeout):
    """Run argv with a hard timeout, killing the whole process group.

    `start_new_session=True` puts the child in its own process group, so
    `os.killpg` takes down grandchildren too (snapper's D-Bus helpers etc.).
    Returns (stdout, stderr, kind, code): kind is "" (ran), "missing" (could
    not start) or "timeout"; code is the exit status (-1 when unknown).
    Never raises.
    """
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError):
        return "", "", "missing", -1
    try:
        out, err = proc.communicate(timeout=timeout)
        return (out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                "", proc.returncode)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, TypeError):
            os.killpg(proc.pid, 15)  # SIGTERM to the group
        try:
            out, err = proc.communicate(timeout=REAP_TIMEOUT)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError, TypeError):
                os.killpg(proc.pid, 9)  # SIGKILL
            out, err, code = b"", b"", -1
            with contextlib.suppress(Exception):
                proc.wait(timeout=REAP_TIMEOUT)  # reap; never leave a zombie
        return (out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                "timeout", code)


# ---------------------------------------------------------------------------
# Source: snapper configs (works for every user, verified live)
# ---------------------------------------------------------------------------
def fetch_configs(ctx):
    """`snapper --jsonout list-configs`. Global options go BEFORE the command;
    `list-configs --jsonout` is a usage error (tested on snapper 0.13.2)."""
    if ctx.mock:
        return ctx.mock.list_configs()  # already (configs, error)
    out, err, kind, _code = run_cmd(["snapper", "--jsonout", "list-configs"],
                                    SNAPPER_TIMEOUT)
    if kind == "missing":
        return None, "snapper is not installed"
    if kind == "timeout":
        return None, "snapper list-configs timed out"
    try:
        data = json.loads(out)
    except ValueError:
        return None, "snapper list-configs produced invalid JSON"
    configs = []
    if isinstance(data, dict):
        raw = data.get("configs")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("config"), str):
                    name = item["config"].strip()
                    if CONFIG_NAME_RE.match(name):
                        configs.append({
                            "name": name,
                            "subvolume": clip(item.get("subvolume"), 120),
                        })
    return configs, ("" if configs else "snapper reported no usable configs")


def read_config_values(ctx, name):
    """Retention values from /etc/snapper/configs/<name> (world-readable)."""
    if ctx.mock:
        text = ctx.mock.config_text(name)
        if text is None:
            return {}
        return parse_config_values(text)
    path = Path("/etc/snapper/configs") / name
    if not CONFIG_NAME_RE.match(name):  # never built from unvalidated names
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return {}
    return parse_config_values(text)


def parse_config_values(text):
    """KEY=VALUE lines from a snapper config file; comments ignored."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def parse_limit(raw):
    """NUMBER_LIMIT may be a number ("5") or a range ("10-50"). We report the
    upper bound (the hard cap after which deletion is unconditional, per
    snapper-configs(5)); None if it is not a usable non-negative integer."""
    if not raw:
        return None
    m = re.search(r"(\d+)\s*-\s*(\d+)$", raw)
    if m:
        return max(int(m.group(1)), int(m.group(2)))
    m = re.search(r"^\s*(\d+)\s*$", raw)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Source: tier B - full snapper list (only after a manual one-time setup)
# ---------------------------------------------------------------------------
def find_snapshot_dicts(data):
    """Tolerant scan: snapper's --jsonout layout has changed between versions,
    so we accept any list of dicts that carry `number` and `description`,
    wherever it sits in the document."""
    found = []
    if isinstance(data, dict):
        for value in data.values():
            found.extend(find_snapshot_dicts(value))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                if "number" in item and "description" in item:
                    found.append(item)
                else:
                    found.extend(find_snapshot_dicts(item))
    return found


def parse_snapper_list(data):
    snapshots = []
    for item in find_snapshot_dicts(data):
        number = parse_snapshot_number(item.get("number"))
        if number is None:
            continue
        snapshots.append({
            "config": None,      # filled in by the caller
            "number": number,
            "date_raw": clip(item.get("date"), 64),
            "date_epoch": parse_date_epoch(item.get("date")),
            "description": clip(item.get("description")),
            "type": clip(item.get("type"), 24),
            "cleanup": clip(item.get("cleanup"), 24) or None,
            "user": clip(item.get("user"), 64),
            "pre_number": parse_snapshot_number(item.get("pre-number")),
            "boot_entry": None,  # filled in by the merge step
            "source": "snapper",
        })
    return snapshots


def parse_snapshot_number(raw):
    """Snapshot numbers arrive as int or str; anything else (lists, dicts,
    bools, None) is junk and dropped rather than coerced."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    m = re.search(r"\d+", raw)
    return int(m.group(0)) if m else None


def parse_date_epoch(raw):
    """Local-time `YYYY-MM-DD HH:MM:SS` -> epoch seconds, else None.
    (limine.conf entry names are local time, matching what the user sees.)"""
    if not raw:
        return None
    m = DATE_RE.match(str(raw).strip())
    if not m:
        return None
    year, month, day, hour, minute, sec = (int(m.group(i)) for i in range(1, 7))
    import calendar
    if not (1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]
            and hour < 24 and minute < 60 and sec < 61):
        return None
    try:
        return int(time.mktime(time.struct_time(
            (year, month, day, hour, minute, sec, 0, 0, -1))))
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Source: tier A - /boot/limine.conf (world-readable, works out of the box)
# ---------------------------------------------------------------------------
def find_limine_conf(ctx):
    """ESP path candidates; /etc/default/limine may override (ESP_PATH=...)."""
    candidates = []
    try:
        text = Path("/etc/default/limine").read_text(
            encoding="utf-8", errors="replace")[:65536]
        for line in text.splitlines():
            m = re.match(r'^\s*ESP_PATH\s*=\s*"?([^"\n]+)"?\s*$', line)
            if m:
                candidates.append(m.group(1).strip().rstrip("/") + "/limine.conf")
    except OSError:
        pass
    candidates.extend(LIMINE_CONF_CANDIDATES)
    if ctx.mock:
        text = ctx.mock.limine_text()
        return ("mock/limine.conf", text) if text is not None else (None, None)
    for path in candidates:
        if not path.startswith("/"):
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_LIMINE_BYTES)
        except OSError:
            continue
        return path, raw.decode("utf-8", "replace")
    return None, None


def parse_limite_conf(text):
    """Extract ONLY number/date/description from the Snapshots submenu.

    limine.conf shape (as written by limine-snapper-sync):
        //Snapshots
        ### Auto-generated by limine-snapper-sync
        comment: 5 / 5 snapshots          <- informational header (not shown)
        ///2026-11-10 20:15:04            <- one entry per snapshot
        comment: 4.5.0-3                  <- description
        ////linux-omarchy                 <- kernel entries (ignored)
        cmdline: ... rootflags=subvol=/@/.snapshots/90/snapshot ...
    Every line that is not `///date`, a description comment or a cmdline
    holding `/.snapshots/<N>/snapshot` is discarded immediately.
    """
    entries = []
    saw_section = False
    current = None
    saw_kernel = False

    def finish(entry):
        if entry and entry.get("number") is not None:
            entries.append(entry)

    for raw in text.splitlines():
        if LIMITE_HEADER_COUNT_RE.match(raw) or LIMITE_SECTION_RE.match(raw):
            saw_section = True
            current = None
            continue
        m = LIMITE_ENTRY_RE.match(raw)
        if m:
            finish(current)
            current = {"date_raw": clip(m.group(1), 64),
                       "description": None, "number": None}
            saw_kernel = False
            continue
        if current is None:
            continue
        if LIMITE_KERNEL_RE.match(raw):
            saw_kernel = True
        mnum = SNAP_NUM_RE.search(raw)
        if mnum and current["number"] is None:
            current["number"] = int(mnum.group(1))
        if not saw_kernel:
            cm = LIMITE_COMMENT_RE.match(raw)
            if cm and current["description"] is None:
                current["description"] = clip(cm.group(1))
    finish(current)

    # de-duplicate by number (first occurrence wins), newest first
    seen = set()
    unique = []
    for entry in sorted(entries, key=lambda e: e["number"], reverse=True):
        if entry["number"] in seen:
            continue
        seen.add(entry["number"])
        unique.append({
            "config": None,
            "number": entry["number"],
            "date_raw": entry["date_raw"],
            "date_epoch": parse_date_epoch(entry["date_raw"]),
            "description": entry["description"],
            "type": None,
            "cleanup": None,
            "user": None,
            "pre_number": None,
            "boot_entry": True,
            "source": "limine",
        })
    status = "ok" if unique else ("empty" if saw_section else "no-section")
    return unique, status


# ---------------------------------------------------------------------------
# Sources: filesystem usage (no subprocesses at all)
# ---------------------------------------------------------------------------
def fs_usage(path="/"):
    """Root filesystem usage from statvfs. On btrfs `f_bavail` is the space a
    normal user can actually use; the kernel reserves some blocks."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    fr = st.f_frsize or st.f_bsize or 4096
    size = fr * st.f_blocks
    avail = fr * st.f_bavail
    used = max(0, size - avail)
    return {"path": path, "size": size, "used": used, "avail": avail,
            "percent_used": round(100.0 * used / size, 1) if size else 0.0}


def find_esp_mount():
    """Find a mounted vfat partition (the ESP) from /proc/self/mountinfo."""
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if " - " not in line:
                    continue
                left, right = line.split(" - ", 1)
                fs_fields = right.split()
                if fs_fields and fs_fields[0].lower() in ("vfat", "msdos"):
                    fields = left.split()
                    if len(fields) > 4:
                        return fields[4]
    except OSError:
        pass
    return None


def esp_usage(ctx):
    """ESP usage + the limine-snapper-sync usage limit (LIMIT_USAGE_PERCENT)."""
    if ctx.mock:
        return ctx.mock.esp_usage()
    mount = find_esp_mount()
    if not mount:
        return None
    usage = fs_usage(mount)
    if usage is None:
        return None
    usage["limit_percent"] = read_esp_limit()
    return usage


def read_esp_limit():
    """LIMIT_USAGE_PERCENT from /etc/limine-snapper-sync.conf (readable); this
    is the point above which limine-snapper-sync stops adding boot entries."""
    try:
        text = Path("/etc/limine-snapper-sync.conf").read_text(
            encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return DEFAULT_ESP_WARN_PERCENT
    values = parse_config_values(text)
    try:
        limit = int(values.get("LIMIT_USAGE_PERCENT", ""))
    except ValueError:
        return DEFAULT_ESP_WARN_PERCENT
    return min(99, max(1, limit))


# ---------------------------------------------------------------------------
# Pure logic: merge, classify, retention message
# ---------------------------------------------------------------------------
def merge_snapshots(configs, tier_b_rows, tier_a_rows):
    """Tier B (full list) wins; tier A supplies the `boot_entry` flag. With
    several configs the rows keep their config name; tier A rows cannot be
    attributed to a config when more than one config exists."""
    by_number = {}
    for row in tier_a_rows:
        by_number.setdefault(row["number"], True)
    if tier_b_rows:
        merged = []
        for row in tier_b_rows:
            row = dict(row)
            row["boot_entry"] = by_number.get(row["number"], False)
            merged.append(row)
        merged.sort(key=lambda r: (-(r["date_epoch"] or 0), -r["number"]))
        return merged
    merged = []
    single = len(configs) == 1
    for row in tier_a_rows:
        row = dict(row)
        row["config"] = row["config"] or (configs[0]["name"] if single else None)
        merged.append(row)
    merged.sort(key=lambda r: (-(r["date_epoch"] or 0), -r["number"]))
    return merged


def classify(configs, snapshots, fs, thresholds, limine_status, tier_b_ok, now):
    """Overall bar state. Order: unavailable > lowdisk > stale > ok.

    - unavailable: no snapper configs (or snapper missing) with no snapshot
      data from any source, OR configs exist but we cannot read snapshots
      from either tier - the widget must not look healthy (feedback: never
      show a healthy-looking widget when we simply cannot see).
    - lowdisk wins over stale: a nearly full filesystem is more urgent.
    - stale is mild by design (feedback E): a dimmed clock, never red.
      stale_days 0 disables the stale state entirely.
    """
    if not configs and not snapshots:
        return "unavailable"
    if not snapshots and limine_status not in ("ok", "empty") and not tier_b_ok:
        return "unavailable"
    if fs and fs.get("percent_used", 0) >= thresholds["disk_warn_percent"]:
        return "lowdisk"
    stale_days = thresholds["stale_days"]
    if stale_days > 0:
        newest = max((s["date_epoch"] for s in snapshots if s["date_epoch"]), default=None)
        if newest is None or now - newest > stale_days * 86400:
            return "stale"
    return "ok"


def newest_age(snapshots, now):
    epochs = [s["date_epoch"] for s in snapshots if s["date_epoch"]]
    if not epochs:
        return None
    return max(0, now - max(epochs))


def retention_lines(count, count_is_subset, limit, limit_important, oldest_number):
    """One or two plain sentences for the confirmation dialog (feedback B).

    Verified against snapper-configs(5): the `number` cleanup keeps the
    YOUNGEST NUMBER_LIMIT snapshots (important ones, counted independently,
    up to NUMBER_LIMIT_IMPORTANT) and never deletes snapshots younger than
    NUMBER_MIN_AGE. Omarchy sets NUMBER_MIN_AGE=0, so once the limit is
    reached the oldest snapshot goes on the next creation.
    """
    parts = []
    if count is not None:
        have = "at least %d" % count if count_is_subset else "%d" % count
        if limit is not None and count < limit:
            parts.append("You have %s of %d snapshots - under the limit, so "
                         "cleanup should not remove any existing snapshot."
                         % (have, limit))
        elif limit is not None:
            parts.append("You have %s of %d snapshots." % (have, limit))
            oldest = " (#%d)" % oldest_number if oldest_number else ""
            parts.append("Creating a new one runs Omarchy's cleanup, which "
                         "keeps only the youngest %d and will likely remove "
                         "the oldest%s." % (limit, oldest))
        else:
            parts.append("You have %s snapshots (limit unknown)." % have)
    elif limit is not None:
        parts.append("Snapshot count unknown; the limit is %d. Creating a new "
                     "one runs Omarchy's cleanup, which keeps only the "
                     "youngest %d and may remove older snapshots." % (limit, limit))
    else:
        parts.append("Creating a snapshot runs Omarchy's cleanup, which may "
                     "remove older snapshots (limit unknown).")
    if limit_important:
        parts.append("Important snapshots (marked in Snapper) are counted "
                     "separately, up to %d." % limit_important)
    return parts


# ---------------------------------------------------------------------------
# State, locking, mock
# ---------------------------------------------------------------------------
def default_state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "omarchy" / "settings" / PLUGIN_ID


class Lock:
    """Non-blocking lock so several bars or a shell restart never run the
    helper concurrently. A held lock produces {"busy": true} in the panel."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "lock"
        self.handle = None

    def __enter__(self):
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "w")
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self.handle:
                with contextlib.suppress(OSError):
                    self.handle.close()
            self.handle = None
        return self

    def __exit__(self, *exc):
        if self.handle:
            with contextlib.suppress(OSError):
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                self.handle.close()
        return False


class Mock:
    """Fixture-based replacement for every real source. Everything downstream
    (parsing, merging, classification, messages) runs for real, in an isolated
    state directory."""

    def __init__(self, name):
        if not re.match(r"^[a-z0-9-]+$", name or ""):
            raise ValueError("invalid mock name: %r" % (name,))
        self.dir = MOCK_DIR / name
        if not self.dir.is_dir():
            available = ", ".join(sorted(p.name for p in MOCK_DIR.iterdir()
                                         if p.is_dir())) if MOCK_DIR.is_dir() else "(none)"
            raise ValueError("unknown mock scenario %r; available: %s"
                             % (name, available))
        self.scenario = read_json(self.dir / "scenario.json", {})

    def list_configs(self):
        return self.scenario.get("configs", []), self.scenario.get("configs_error", "")

    def config_text(self, name):
        return self.scenario.get("config_files", {}).get(name)

    def limine_text(self):
        """Template mode: {DATEk} placeholders are rendered with dates relative
        to *now*, so fixtures never go stale. 'static' mode reads a plain file.
        Returns None = the file is absent (limine-sync not installed)."""
        sc = self.scenario
        if sc.get("limine") == "static":
            path = self.dir / "limine.conf"
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
            return None
        template = sc.get("limine_template")
        if template is None:
            return None
        ages = sc.get("limine_ages_hours", [])
        text = template
        for i, hours in enumerate(ages):
            text = text.replace("{DATE%d}" % i, self.fmt_date(hours))
        return text

    def fmt_date(self, hours_ago):
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(time.time() - hours_ago * 3600.0))

    def tier_b(self, name):
        """('ok', rows) | ('denied', None) | ('missing', None) | ..."""
        tb = self.scenario.get("tier_b", "denied")
        if isinstance(tb, dict):
            ages = self.scenario.get("tier_b_ages_hours", [])
            rows = []
            for i, snap in enumerate(tb.get("snapshots", [])):
                s = dict(snap)
                date = str(s.get("date", ""))
                m = re.match(r"\{DATE(\d+)\}", date)
                if m:
                    s["date"] = self.fmt_date(ages[int(m.group(1))])
                rows.append(s)
            parsed = parse_snapper_list({"snapshots": rows})
            for row in parsed:
                row["config"] = name
            return "ok", parsed
        return tb, None

    def fs_usage(self):
        return self.scenario.get("fs")

    def esp_usage(self):
        return self.scenario.get("esp")


class Context:
    def __init__(self, state_dir, cfg, mock_name, force):
        self.state_dir = Path(state_dir)
        self.cfg = cfg
        self.force = force
        self.mock = Mock(mock_name) if mock_name else None

    @property
    def thresholds(self):
        return {
            "stale_days": clamp_int(self.cfg.get("staleDays", DEFAULT_STALE_DAYS), 0, 3650, DEFAULT_STALE_DAYS),
            "disk_warn_percent": clamp_int(self.cfg.get("diskWarnPercent", DEFAULT_DISK_WARN_PERCENT), 1, 99, DEFAULT_DISK_WARN_PERCENT),
            "esp_warn_percent": clamp_int(self.cfg.get("espWarnPercent", 0), 0, 99, 0),
        }


def clamp_int(raw, low, high, default):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
def empty_doc():
    return {
        "schema": SCHEMA, "version": VERSION, "plugin": PLUGIN_ID,
        "state": "unknown", "checked_at": None, "busy": False, "mock": None,
        "errors": [], "warnings": [],
        "tiers": {"limine": {"status": "unknown", "path": None},
                  "snapper_full": "unknown"},
        "configs": [], "snapshots": [],
        "counts": {"total": 0, "boot_entries": 0, "configs": 0},
        "newest": None,
        "fs": None, "esp": None,
        "show_config_column": False,
        "creation": {"command": CREATION_COMMAND, "retention_lines": []},
    }


def add_error(doc, source, kind, message):
    if len(doc["errors"]) < MAX_ERRORS:
        doc["errors"].append({"source": source, "kind": kind,
                              "message": clip(message, 160)})


def build_doc(ctx):
    doc = empty_doc()
    doc["mock"] = ctx.mock.scenario.get("name") if ctx.mock else None
    now = time.time()
    doc["checked_at"] = now
    th = ctx.thresholds

    # --- configs -------------------------------------------------------
    configs, config_error = fetch_configs(ctx)
    if config_error:
        kind = ("missing" if "not installed" in config_error
                else "no-configs" if "no configs" in config_error else "error")
        add_error(doc, "configs", kind, config_error)
    doc["configs"] = configs or []
    doc["counts"]["configs"] = len(doc["configs"])

    # --- retention per config -----------------------------------------
    limit = None
    limit_important = None
    oldest_number = None
    for cfg in doc["configs"]:
        values = read_config_values(ctx, cfg["name"])
        cfg["limit"] = parse_limit(values.get("NUMBER_LIMIT"))
        cfg["limit_important"] = parse_limit(values.get("NUMBER_LIMIT_IMPORTANT"))
        cfg["number_cleanup"] = str(values.get("NUMBER_CLEANUP", "")).lower() == "yes"
        if cfg["limit"] is not None:
            limit = cfg["limit"] if limit is None else max(limit, cfg["limit"])
        if cfg["limit_important"] is not None:
            limit_important = (cfg["limit_important"] if limit_important is None
                               else max(limit_important, cfg["limit_important"]))

    # --- tier A: limine.conf ------------------------------------------
    path, text = find_limine_conf(ctx)
    doc["tiers"]["limine"]["path"] = path
    tier_a_rows = []
    if text is None:
        doc["tiers"]["limine"]["status"] = "missing"
    else:
        tier_a_rows, status = parse_limite_conf(text)
        doc["tiers"]["limine"]["status"] = status

    # --- tier B: full snapper list (cached probe) ----------------------
    tier_b_rows = []
    tier_b_statuses = []
    tier_b_ok = False
    if doc["configs"]:
        state = load_state(ctx)
        cache = state.setdefault("tier_b", {})
        save_cache = False
        for cfg in doc["configs"]:
            name = cfg["name"]
            entry = cache.get(name)
            # Only *failures* are cached: a fresh probe after a denial would
            # just fail again every poll, but an `ok` probe is a cheap local
            # D-Bus call whose rows we want every time.
            if entry and entry.get("status") != "ok" and not ctx.force and \
                    0 <= now - entry.get("checked_at", 0) < TIER_B_PROBE_INTERVAL:
                status_b, rows = entry.get("status"), None
            else:
                status_b, rows = probe_tier_b(ctx, name)
                cache[name] = {"status": status_b, "checked_at": now}
                save_cache = True
            tier_b_statuses.append(status_b)
            if status_b == "ok" and rows:
                tier_b_ok = True
                tier_b_rows.extend(rows)
        if save_cache:
            write_json_atomic(ctx.state_dir / "state.json", state)
    if "ok" in tier_b_statuses:
        doc["tiers"]["snapper_full"] = "ok"
    elif tier_b_statuses:
        doc["tiers"]["snapper_full"] = tier_b_statuses[0]
    else:
        doc["tiers"]["snapper_full"] = "no-configs"

    # --- merge ---------------------------------------------------------
    snapshots = merge_snapshots(doc["configs"], tier_b_rows, tier_a_rows)
    doc["snapshots"] = snapshots
    doc["counts"]["total"] = len(snapshots)
    doc["counts"]["boot_entries"] = sum(1 for s in snapshots if s.get("boot_entry"))
    doc["show_config_column"] = len(doc["configs"]) > 1
    age = newest_age(snapshots, now)
    newest = None
    if snapshots:
        newest_row = snapshots[0]  # merge sorts newest first
        newest = {"number": newest_row["number"],
                  "age_seconds": age,
                  "date_epoch": newest_row["date_epoch"]}
    doc["newest"] = newest

    # --- filesystem + ESP ---------------------------------------------
    doc["fs"] = ctx.mock.fs_usage() if ctx.mock else fs_usage("/")
    if doc["fs"]:
        doc["fs"]["type"] = "btrfs"
    esp = ctx.mock.esp_usage() if ctx.mock else esp_usage(ctx)
    if esp and th["esp_warn_percent"] > 0:
        esp["warn_percent"] = th["esp_warn_percent"]
    elif esp:
        esp["warn_percent"] = esp.get("limit_percent", DEFAULT_ESP_WARN_PERCENT)
    doc["esp"] = esp

    # --- classification ------------------------------------------------
    limine_status = doc["tiers"]["limine"]["status"]
    doc["state"] = classify(doc["configs"], snapshots, doc["fs"], th,
                            limine_status, tier_b_ok, now)
    if esp and esp.get("percent_used", 0) >= esp.get("warn_percent", 100):
        doc["warnings"].append({
            "kind": "esp_full",
            "message": clip("Boot partition %d%% used (limit %d%%) - "
                            "limine-snapper-sync stops adding snapshot boot "
                            "entries above its limit."
                            % (esp.get("percent_used", 0),
                               esp.get("warn_percent", 100)), 200)})

    # --- creation info (constant command + retention warning) ----------
    count = len(tier_b_rows) if tier_b_rows else (len(tier_a_rows) if tier_a_rows else None)
    oldest = snapshots[-1]["number"] if snapshots else None
    doc["creation"]["retention_lines"] = retention_lines(
        count, bool(tier_a_rows and not tier_b_rows), limit, limit_important, oldest)
    return doc


def probe_tier_b(ctx, name):
    """One `snapper -c <cfg> list` probe. Denied caching happens in build_doc."""
    if ctx.mock:
        return ctx.mock.tier_b(name)
    out, err, kind, code = run_cmd(["snapper", "--jsonout", "-c", name, "list"],
                                   SNAPPER_TIMEOUT)
    if kind == "missing":
        return "missing", None
    if kind == "timeout":
        return "timeout", None
    if code != 0:
        if "permission" in (out + err).lower():
            return "denied", None
        return "error", None
    try:
        data = json.loads(out)
    except ValueError:
        return "error", None
    rows = parse_snapper_list(data)
    for row in rows:
        row["config"] = name
    return "ok", rows


def load_state(ctx):
    state = read_json(ctx.state_dir / "state.json", {})
    if state.get("schema") != 1 or not isinstance(state.get("tier_b"), dict):
        # unusable or newer schema: start clean (state is only an optimisation)
        return {"schema": 1, "tier_b": {}}
    return state


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    state_dir = default_state_dir()
    mock_name = None
    config_json = "{}"
    force = False
    positional = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--state-dir":
            i += 1
            if i >= len(args):
                return usage_error("--state-dir needs a value")
            state_dir = Path(args[i])
        elif arg.startswith("--state-dir="):
            state_dir = Path(arg.split("=", 1)[1])
        elif arg == "--mock":
            i += 1
            if i >= len(args):
                return usage_error("--mock needs a value")
            mock_name = args[i]
        elif arg.startswith("--mock="):
            mock_name = arg.split("=", 1)[1]
        elif arg == "--config":
            i += 1
            if i >= len(args):
                return usage_error("--config needs a value")
            config_json = args[i]
        elif arg.startswith("--config="):
            config_json = arg.split("=", 1)[1]
        elif arg == "--force":
            force = True
        elif arg == "--version":
            print(VERSION)
            return 0
        elif arg == "check":
            positional.append(arg)
        else:
            return usage_error("unknown argument: %r" % arg)
        i += 1

    if positional != ["check"]:
        return usage_error("usage: btrfs_snapshots.py [options] check")

    try:
        cfg = json.loads(config_json)
        if not isinstance(cfg, dict):
            raise ValueError
    except ValueError:
        return usage_error("invalid --config JSON")

    if mock_name:
        # Mocks must never read or write the real cache: a real tier-B denial
        # cached earlier would otherwise leak into mock scenarios (and vice
        # versa). Isolated subdir, still under --state-dir when given.
        state_dir = Path(state_dir) / ("mock-" + mock_name)
    try:
        ctx = Context(state_dir, cfg, mock_name, force)
    except ValueError as exc:
        return usage_error(str(exc))
    with Lock(state_dir) as lock:
        if not lock.handle:
            doc = empty_doc()
            doc["busy"] = True
            doc["checked_at"] = time.time()
        else:
            doc = build_doc(ctx)
    json.dump(doc, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def usage_error(message):
    print("btrfs_snapshots.py: %s" % message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
