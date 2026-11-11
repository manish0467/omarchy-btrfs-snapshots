# Design — io.github.manish0467.btrfs-snapshots

Status: implemented as designed (v0.1.0). This document records the approved design
and the reasoning; the full privilege statement lives in the README ("What this
plugin runs, reads and writes").

## Context

- Omarchy 4.5.0-3, Quickshell "Quattro" shell, Arch Linux; target machines run
  Omarchy's default setup: Snapper config `root` on subvolume `/`, retention
  `NUMBER_LIMIT=5`, no timeline, daily cleanup timer, limine-snapper-sync keeping
  snapshot boot entries in the Limine boot config. Must degrade gracefully without
  Snapper, without configs, without limine-snapper-sync, or without permissions.
- Omarchy's own tooling (verified by reading the installed scripts):
  - `omarchy-snapshot create` loops over all Snapper configs, creates one snapshot
    per config with cleanup algorithm `number`, described by the current Omarchy
    version, then runs the number cleanup. Takes no user description.
  - `omarchy-update` calls the above before every update, tolerating failure.
  - `omarchy-snapshot restore` delegates to the limine-snapper-sync restore tool.
  - Omarchy's Snapper template: `NUMBER_CLEANUP=yes`, `NUMBER_LIMIT=5`,
    `NUMBER_LIMIT_IMPORTANT=5`, `NUMBER_MIN_AGE=0`, `TIMELINE_CREATE=no`.
- Value-add of this plugin (no duplication): at-a-glance snapshot health (count,
  age, staleness), disk and boot-partition pressure visibility, a confirmed
  pre-risky-change snapshot button, and a searchable snapshot history. Omarchy's
  menu offers create/restore but nothing monitors or lists.

## Privilege model (every row verified live as a normal user)

| Source | Without setup | Notes |
|---|---|---|
| `snapper list-configs` | yes | Global JSON options go *before* the command. |
| `snapper -c <cfg> list` | no | Denied on stock installs (D-Bus "No permissions"). Probed lazily, denials cached 1 h. |
| `/.snapshots/` directory | no | Root-only directory mode. |
| `/etc/snapper/configs/<cfg>` | yes | Retention values for the warning. |
| Limine boot config (ESP) | yes | World-readable; contains the boot-menu snapshot view. The key tier-A source. |
| ESP usage | yes | vfat mount found from mountinfo; `statvfs`. |
| Root filesystem usage | yes | `statvfs("/")` — no subprocess at all. |
| Btrfs qgroups / per-snapshot sizes | no | Quotas off on stock installs; enabling is a system change the plugin never makes → honest "unavailable". |

**Tier A (out of the box):** boot-config snapshot view — number (from the snapshot
subvolume reference in each entry's kernel command line), date (entry name),
description (comment), plus filesystem/ESP usage. Only those three fields are ever
extracted; every other byte is discarded immediately (privacy, feedback C).

**Tier B (opt-in, user-performed, documented in README only):** full Snapper list
with type, cleanup, user. Neutrally documented as a trade-off (feedback D): it grants
the user's account — and everything running as it — snapshot management without
authentication.

## Architecture

Same shape as the arch-updates plugin: thin QML, one stdlib-only Python helper, mock
fixtures, unit tests.

```
                         ┌────────────────────────────────────────────────┐
                         │ Panel.qml (data owner)                         │
  shell.json (settings) ─┤  • settings via setting() -> JSON arg          │
                         │  • Process: helper check                       │
                         │  • FileView watch boot config (inotify)        │
                         │  • Timer: slow poll; post-creation refreshes   │
                         │  • ConfirmDialog -> bar.run(constant command)  │
                         └───────┬─────────────────────────▲──────────────┘
              argv (list         │ JSON stdout             │ open/close contract
              elements only)     ▼                         │ BarWidget.qml ⇄ shell
                         ┌────────────────────────────────────────────────┐
                         │ helper/btrfs_snapshots.py  (stdlib only)       │
                         │  check:                                        │
                         │   A. boot config parse (number/date/desc only) │
                         │   B. snapper full list (tier B, cached probe)  │
                         │   C. snapper list-configs                      │
                         │   D. /etc/snapper/configs/* (retention)        │
                         │   E. os.statvfs("/") and ESP vfat mount        │
                         │   F. sync tool config (ESP usage limit)        │
                         │  state.json: cached probe statuses only        │
                         └────────────────────────────────────────────────┘
  the only state-changing action (in the user's terminal, after confirmation):
    bar.run("omarchy-launch-floating-terminal-with-presentation omarchy-snapshot create")
```

### Key decisions

- **D1 — Two tiers, honestly labelled.** Tier A always works; tier B only when the
  user opted in. The panel states which is active; when neither is readable the
  panel says so plainly and points at the README (feedback: never a blank panel,
  never a workaround). *Rejected:* any privilege elevation, parsing unreadable
  directories, D-Bus tricks.
- **D2 — Creation is a constant.** The confirmation dialog shows the exact command;
  Cancel is the default; the command runs in the floating terminal (authentication
  happens there). No description field in v0.1.0 (feedback Q1): user text never
  enters any command string. *Rejected:* per-config command construction, wrappers.
- **D3 — Bar count semantics.** Count = snapshots visible to the plugin (tier A:
  boot entries; tier B: full list), explained in the tooltip. The boot config's
  own "N / M snapshots" header is treated as informational only and never displayed
  (its semantics are undocumented).
- **D4 — Event-driven where possible.** Refresh on panel open, on boot-config
  changes (FileView inotify, 10 s debounce), slow 30-min poll (floor 10), two
  one-shot refreshes after a confirmed creation, manual refresh. One check at a
  time per widget + helper file lock; QML kill timer (30 s) and helper subprocess
  timeouts (5 s, process-group kill) so the shell never hangs.
- **D5 — Sizes.** Always honest "unavailable (quotas off)" unless obtainable. No
  scans without a user click anywhere in v1. The number's meaning (exclusive vs
  referenced) is explained in one sentence in the panel.
- **D6 — Strictly read-only.** No delete/restore/cleanup/quota changes. Omarchy's
  own restore path is displayed as text; destructive commands exist only in the
  README (feedback A), so interface code carries no privilege-flavoured strings.
- **D7 — Helper language.** Python 3 stdlib + QML; 3.9-compatible syntax; tests on
  the current Python and on 3.9 via `uv run --no-project --python 3.9` (feedback G).
- **D8 — Retention warning.** Built from the Snapper config's own values, wording
  verified against snapper-configs(5) (number cleanup keeps the youngest
  NUMBER_LIMIT; important ones counted separately up to NUMBER_LIMIT_IMPORTANT;
  nothing younger than NUMBER_MIN_AGE is deleted; Omarchy sets MIN_AGE=0). Tier-A
  counts are phrased "at least N" because boot entries are a subset; unknown counts
  get a generic warning.
- **D9 — Stale is mild** (feedback E): default 7 d, 0 disables, dimmed clock glyph,
  never red. Low-disk stays urgent and takes precedence.
- **D10 — Synthetic fixtures** (feedback C): all mock data and screenshots use
  sanitized structure-only content; dates are rendered fresh at load time from age
  lists so fixtures never go stale.

## Failure modes → visible states

| Situation | Display |
|---|---|
| Snapper not installed / no configs | `unavailable`; panel explains and points at Omarchy's own setup path |
| No readable source at all | `unavailable`; panel states it plainly + README pointer for the optional manual setup |
| Boot config missing/changed format/no section | Tier A marked accordingly; tier B or unavailable as appropriate |
| Creation refused/cancelled | The terminal shows the tool's own output; the panel just re-checks later |
| Filesystem nearly full | `lowdisk` bar state + red summary line |
| Boot partition at/above the sync tool's limit | Degraded marker + explicit panel note |
| Helper timeout/crash | `error` state with reason; retry allowed; shell never blocks |

## Layout

See the repository tree; docs/TEST_PLAN.md lists fixtures, scenarios and coverage.
