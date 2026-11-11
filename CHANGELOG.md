# Changelog

All notable changes to this project are documented in this file.
The format is based on Keep a Changelog; versioning is SemVer.

## [Unreleased]

### Fixed
- The bar button's count/age label ("5·18h") overflowed the fixed width of
  one status slot, so the label, glyph and the neighbouring widgets drew on
  top of each other. The button now measures its label and widens itself to
  fit (labels are hidden on vertical bars, as the shell's media widget does).

## [0.1.0] - 2026-11-11

Initial release.

### Added
- Bar widget showing snapshot count and the age of the newest snapshot
  ("5 · 2h"), with ok / stale (mild) / low-disk / unavailable states and a
  degraded marker when the boot partition reaches the limine-snapper-sync
  usage limit.
- Panel listing snapshots (number, date, description; type, cleanup
  algorithm, user and boot-menu badge where available) with a filter box,
  newest first. Two privilege tiers, labelled honestly:
  - Tier A, out of the box: boot-menu snapshot view parsed from the
    world-readable Limine boot config (written by limine-snapper-sync).
  - Tier B, only after a manual, user-chosen Snapper permission change
    (documented in the README, never performed by the plugin): the full
    `snapper list`.
- Filesystem summary: root Btrfs used/free, boot partition usage against the
  limine-snapper-sync limit, and an honest "sizes unavailable (quotas off)"
  note (no expensive scans, no hidden privilege use).
- "Create snapshot" button that shows the exact command in a Cancel-default
  confirmation dialog together with a retention warning (verified against
  snapper-configs(5): number cleanup keeps the youngest NUMBER_LIMIT), then
  runs Omarchy's own `omarchy-snapshot create` in the floating terminal.
  The command string is a compile-time constant; the plugin never elevates.
- Read-only everywhere else: no delete, no restore/rollback, no cleanup, no
  quota changes in v1. Omarchy's own restore path is displayed as text; all
  destructive commands are documented in this README only.
- Mock modes for every state (normal, many, none, no-snapper, no-configs,
  denied, lowdisk, stale, esp-full, full) with synthetic, sanitized fixtures.
- Unit tests running on the current Python and on Python 3.9 (via uv).
