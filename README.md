# Btrfs Snapshots (Omarchy plugin)

`io.github.manish0467.btrfs-snapshots` — a bar widget with a panel that shows your
Btrfs/Snapper snapshots (number, date, description, boot-menu badge, type and cleanup
algorithm where available) and offers a **confirmed one-click snapshot** before risky
changes, using **Omarchy's own** `omarchy-snapshot create` in your floating terminal.

**Read-only by design.** The plugin never deletes, restores, rolls back or cleans up
anything, never touches quotas, never edits Snapper or bootloader configuration, and
never elevates privileges itself. It only reads world-readable files and runs one
command — in a terminal you control — after an explicit confirmation.

## What you see

- **Bar**: snapshot count and the age of the newest snapshot, e.g. `5 · 2h`. States:
  - `ok` — fresh snapshot, disk fine (icon fades out when idle, configurable).
  - `stale` — no snapshot newer than `staleDays` (default 7, `0` disables). Deliberately
    a *mild* state (dimmed clock icon, never red).
  - `lowdisk` — root filesystem usage at/above `diskWarnPercent` (default 90%). Urgent.
  - Boot partition at/above limine-snapper-sync's usage limit → a degraded marker (`!`)
    on the icon; the tool stops adding snapshot boot entries above that limit.
  - `unavailable` — nothing readable; never looks healthy.
- **Panel**:
  - Filesystem summary: root Btrfs used/free/percent, boot partition usage against the
    tool's own limit, and an honest "snapshot sizes unavailable (quotas off)" note.
  - Snapshot list, newest first, with a filter box: number, date/time, description,
    type/cleanup/config/user where known, and whether the snapshot is a boot entry.
  - "Create snapshot…" → confirmation dialog showing the **exact command** plus a
    retention warning (e.g. "You have 5 of 5 snapshots… will likely remove the
    oldest (#42)") → runs `omarchy-snapshot create` in the floating terminal.
  - "Omarchy's own ways" notes: restore via `omarchy-snapshot restore` or the Limine
    boot menu; destructive actions are only documented here in the README.

## Where the data comes from (privilege tiers, honestly labelled)

Stock Omarchy runs Snapper as root only, so a plugin running as your user cannot read
`/.snapshots` or ask Snapper for the list. The plugin therefore uses two tiers and
always tells you which one you are looking at:

- **Tier A — works out of the box, no privileges.** limine-snapper-sync (installed and
  enabled by Omarchy) writes every bootable snapshot into the Limine boot config,
  which is **world-readable**. The plugin parses only three things out of it: snapshot
  number (from the kernel command line), date (entry name) and description (comment).
  Every other byte (machine-id, UUIDs, kernel parameters) is discarded immediately and
  never logged or stored. Limitation: this is the *boot menu view* — a subset of all
  snapshots (very old snapshots and excluded types are missing, and type/cleanup
  metadata is not in this file).
- **Tier B — only after a change you choose to make yourself.** If your account is
  allowed to list snapshots (see the *optional manual setup* below), the plugin reads
  the full `snapper list`: all snapshots with type (single/pre/post), cleanup
  algorithm, user and description. The plugin never performs that setup; it only
  detects it.

**Sizes:** per-snapshot sizes on Btrfs need qgroups (quotas) or expensive scans. Stock
Omarchy has quotas off and the plugin will not enable them, scan, or elevate — so
sizes are shown as "unavailable (quotas off)". The filesystem summary (used/free) is
taken from the kernel's own accounting (`statvfs`), which is free and exact.

## What this plugin runs, reads and writes

**Runs:**

1. `snapper --jsonout list-configs` — as your user, read-only, with a 5-second
   timeout. Works on every install.
2. `snapper --jsonout -c <config> list` — as your user, read-only, only when a
   previous probe succeeded or permission may have changed (denials are cached for an
   hour; failures never re-probe more often than that). On stock installs Snapper
   denies this and nothing happens.
3. After you press "Create snapshot" **and confirm**, exactly one command in Omarchy's
   floating terminal, where authentication happens in *your* session:
   `omarchy-launch-floating-terminal-with-presentation omarchy-snapshot create`
   The command string is a compile-time constant in `Panel.qml`. You can cancel; the
   plugin never sees credentials, never elevates itself, and contains no code path
   that could (this includes the Python helper and every string it builds).
4. `python3 -I helper/btrfs_snapshots.py check` — the helper itself; stdlib-only,
   isolated mode, process-group killed on timeout.

**Reads (all world-readable, verified):**
- `/boot/limine.conf` (or the ESP path from `/etc/default/limine`) — parsed as tier A.
- `/etc/snapper/configs/<name>` — retention values (`NUMBER_LIMIT`,
  `NUMBER_LIMIT_IMPORTANT`, `NUMBER_MIN_AGE`) for the retention warning.
- `/etc/limine-snapper-sync.conf` — `LIMIT_USAGE_PERCENT` for the boot-partition
  warning.
- `/proc/self/mountinfo` — to find the vfat boot partition for the ESP summary.
- Its own settings from your `shell.json` entry and its state file (below).

**Writes:** only
`~/.local/state/omarchy/settings/io.github.manish0467.btrfs-snapshots/state.json`
(cached probe results: statuses and timestamps only) and a `lock` file next to it.
Nothing else — no config edits, no hooks, no systemd units, no dotfiles.

**Never:** the plugin itself never invokes any privilege-escalation mechanism, never
deletes/restores/cleans up snapshots, never modifies Snapper configs, quotas, the
bootloader, the ESP or any existing snapshot. All destructive operations are things
*you* run yourself; the relevant Omarchy-supported commands are documented below only
in this README (deliberately not in the plugin's interface code).

## Creating snapshots (what actually happens)

1. Click "Create snapshot…" (or press `c` in the panel).
2. The confirmation dialog shows the exact command and a retention warning computed
   from your Snapper config's `NUMBER_LIMIT` — verified against snapper-configs(5):
   the `number` cleanup algorithm keeps the *youngest* NUMBER_LIMIT snapshots (and
   important ones up to NUMBER_LIMIT_IMPORTANT, counted separately) and never deletes
   snapshots younger than `NUMBER_MIN_AGE`. Omarchy sets `NUMBER_MIN_AGE=0`, so once
   the limit is reached the oldest snapshot is removed on the next creation.
3. On confirm, the command opens in the floating terminal; you authenticate there.
4. The panel refreshes when the boot config changes (inotify) or a few minutes later.

There is **no description field in v0.1.0**: Omarchy's own command stamps the Omarchy
version as the description and takes no arguments, and this plugin refuses to put
user-typed text into any command string. Labels for your snapshots are better added
with `omarchy-snapshot`'s own flow (or a later plugin version, if a safe design
appears).

## Restoring, deleting, cleaning up (Omarchy's way — do it yourself)

These are documented here only; the plugin will never run them.

- **Restore** — the supported paths:
  - Omarchy command menu: `omarchy-snapshot restore` (runs Omarchy's restore flow,
    which uses `limine-snapper-restore`), or
  - Reboot, pick a snapshot in the Limine boot menu's Snapshots submenu, and use the
    restore notification that appears after booting it.
- **Delete a snapshot** (and its boot entry, via the sync tool):
  `snapper -c root delete <number>` — as root, in a terminal. The boot entry and its
  kernel files are removed by limine-snapper-sync automatically.
- **Remove boot entries only** (free boot-partition space, keep the snapshot):
  `limine-snapper-remove <first-id>..<last-id>`.
- **Cleanup** is already handled by Omarchy (`snapper-cleanup.timer` keeps the 5
  youngest per its config); you rarely need to run it manually.
- **Inspect sizes yourself** (heavy; not run by the plugin):
  `btrfs filesystem du /` as root (slow, scans extents), or enable quotas yourself
  first if you want Snapper to track them.

## Optional manual setup (tier B) — a trade-off you choose

By default, Snapper denies your user the snapshot list. If you want the full list in
this plugin without granting anything at runtime, you could — entirely at your option,
one time, in your own terminal — allow your account in the Snapper config:

```
snapper -c root set-config ALLOW_USERS="$USER" SYNC_ACL=yes
```

(Repeat for each additional Snapper config, if you create any.)

Understand the trade-off before deciding: this grants your account — and therefore
**every program that ever runs as your account** — the ability to list and manage
root snapshots without further authentication. It also makes `/.snapshots` readable
for your user via synced ACLs. It is a common openSUSE-style setup, but on a
single-user Omarchy laptop the tier-A boot-menu view may be all you ever need. The
plugin works fully without it (it just shows fewer columns and only bootable
snapshots) and will never run, suggest running, or assist with this change beyond
this paragraph. Verify details in the Snapper documentation (`snapper-configs(5)`).

## Installation

```
omarchy plugin add https://github.com/manish0467/omarchy-btrfs-snapshots.git --enable
```

Then move the widget where you like it in the bar layout. Removal:
`omarchy plugin remove io.github.manish0467.btrfs-snapshots` (deletes the folder and
the widget entry; your state folder
`~/.local/state/omarchy/settings/io.github.manish0467.btrfs-snapshots/` can be deleted
by hand afterwards if you want).

## Settings

All optional, in the widget's `shell.json` entry (see `config.example.json`); editing
takes effect within a second, no restart needed.

| Key | Default | Meaning |
|---|---|---|
| `showCount` | `both` | Bar label: `both` / `count` / `age` / `none` |
| `idleBehavior` | `dim` | Bar icon when everything is fine: `dim` / `hide` / `show` |
| `staleDays` | `7` | Days without a fresh snapshot before the mild stale state; `0` disables |
| `diskWarnPercent` | `90` | Root filesystem usage that turns the widget lowdisk |
| `espWarnPercent` | `0` | Boot-partition warning threshold; `0` follows limine-snapper-sync's `LIMIT_USAGE_PERCENT` (usually 85) |
| `pollMinutes` | `30` | Slow poll interval (floor 10 min); refresh also happens on panel open, on limine.conf changes (inotify), and after a confirmed creation |
| `mock` | `""` | Mock scenario for testing (see below); never set in daily use |

## Mock modes (for development and screenshots)

Set `"mock": "<scenario>"` in the widget's `shell.json` entry, open the panel, remove
the key afterwards. Scenarios (all synthetic fixtures, no real system access):
`normal`, `many`, `none`, `no-snapper`, `no-configs`, `denied`, `lowdisk`, `stale`,
`esp-full`, `full`.

## Development

```
python3 -m unittest discover -s tests          # unit tests (also: uv run --no-project --python 3.9 …)
tests/lint.sh                                  # Qt 6 qmllint against the shell's imports
omarchy plugin validate ~/.config/omarchy/plugins/io.github.manish0467.btrfs-snapshots
```

Design decisions, privilege table and the Limine analysis: `docs/DESIGN.md`.
Test plan and fixture inventory: `docs/TEST_PLAN.md`.

## License

MIT — see `LICENSE`. Author: Manish.
