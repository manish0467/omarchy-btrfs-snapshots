# Test plan — io.github.manish0467.btrfs-snapshots

## Commands

```
# unit tests — current Python
python3 -m unittest discover -s tests -v

# unit tests — Python 3.9 (user-level, no project env)
uv run --no-project --python 3.9 python -m unittest discover -s tests -v

# QML lint (Qt 6 qmllint against the shell's imports; see tests/lint.sh header)
tests/lint.sh

# manifest validation
omarchy plugin validate ~/.config/omarchy/plugins/io.github.manish0467.btrfs-snapshots
```

## Principles

- Standard library only in the helper and in the tests. Nothing touches the real
  Snapper, the real boot config, or the real state directory: every CLI test runs
  with `--mock <scenario>` and a temporary `--state-dir`.
- The real-system verification (dev install) exercises listing and dialogs only.
  **No snapshot is ever created on the real system during development or testing** —
  a real creation would trigger Omarchy's cleanup and likely remove the oldest
  snapshot and its boot entry. The creation flow is verified with mocks and through
  Cancel in the confirmation dialog.

## Mock scenarios (`mock/<name>/scenario.json`, all synthetic)

Dates are placeholders rendered fresh at load time from age lists, so fixtures never
go stale. All content is sanitized (structure only — no machine-ids, UUIDs,
hostnames or real kernel versions; snapshot numbers 42–50, descriptions like
`1.2.3-1`).

| Scenario | Data shape | Expected state / assertions |
|---|---|---|
| `normal` | 3 boot entries, fs 62%, ESP 19%, tier B denied | `ok`, count 3, newest hours old |
| `many` | 5 boot entries at retention limit, fs 71%, ESP 78% | `ok`; retention says "at least 5 of 5" + oldest-removal warning |
| `none` | Snapshots section present but empty | `stale` (working source, zero snapshots), count 0 |
| `no-snapper` | configs error: missing; no boot config | `unavailable`, error kind `missing` |
| `no-configs` | configs error: no configs; no boot config | `unavailable`, error kind `no-configs` |
| `denied` | configs fine; tier B denied; no boot config | `unavailable`, tiers.snapper_full `denied` |
| `lowdisk` | fs 93.4% | `lowdisk` (wins over stale) |
| `stale` | newest snapshot ~9 days old | `stale` |
| `esp-full` | ESP 87.1% vs limit 85% | `ok` + `esp_full` warning (degraded marker, not a red state) |
| `full` | tier B ok (4 snapshots) + boot config (3 of them bootable) | `ok`; total 4, boot entries 3, types/cleanup filled, config column values |

## Unit test inventory

`tests/test_helper.py`:
- Limine config parsing: numbers/dates/descriptions extracted, kernel-entry comments
  not mistaken for descriptions, deduplication, header-count line ignored, empty
  section vs no section, entries without a number dropped, unparsable dates kept as
  raw text, privacy (no machine-id/UUID/cmdline bytes in output).
- Date/number/config parsing: local-time date parsing incl. range validation
  (2026-13-99 rejected), snapshot numbers (int/str/None/junk), KEY=VALUE config
  parsing, NUMBER_LIMIT number and range forms.
- Snapper list parsing: realistic string-typed JSON fixture, tolerant nested-scan
  lookup, empty cleanup → None, description clipping.
- Merge/classification: tier-B wins with boot_entry flags, tier-A config
  attribution (single) vs none (multi), all states incl. stale disabled at 0,
  lowdisk precedence, unavailable when no data source is readable.
- Retention message builder: under limit, at limit with oldest number, "at least N"
  for tier-A counts, unknown count/limit variants, important-snapshot note.
- CLI over every mock scenario (the table above), state.json written and
  privacy-safe, bad args exit 2, busy document when the lock is held.

`tests/test_hardening.py`:
- Privilege invariants: no elevation literals anywhere in the helper source; no
  shell execution (`shell=True`, `os.system`, ...); exactly one Popen shape with
  fixed argv; the creation command is the documented constant; no destructive verbs
  in any command-building line.
- Hostile input: command-injection-flavoured limine lines (only the number is
  extracted, rest discarded), NUL/control characters collapsed by the text
  sanitiser, huge inputs bounded, hostile snapper JSON (lists/dicts as numbers
  dropped, deep nesting, dict descriptions stringified), JSON-serialisable output.
- Process hygiene: `run_cmd` rejects string argv (never run), missing binaries
  handled, timeout kills the whole process group (child + grandchild) and leaves no
  zombies.
- State: round-trip, bad-schema repair, garbage tolerance, state.json contains only
  `schema` + `tier_b` (statuses/timestamps — never snapshot content).
- Settings clamping (staleDays 0 disables, out-of-range values clamped).

## Manual verification checklist (dev install)

1. `omarchy plugin validate` passes; `tests/lint.sh` clean.
2. Live widget: count/age shown from the real boot config; panel lists the real
   snapshots; filter works; keyboard `/`, `r`, `c`, Escape, Tab work.
3. Confirmation dialog through **Cancel only** (never confirm on the real system).
4. Mock states via `"mock": "<scenario>"` for: stale, lowdisk, esp-full, denied,
   none, many, full — widget icon/label and panel notes correct.
5. `omarchy plugin disable` → widget disappears; re-enable → returns; shell log
   (`qs log -p "$OMARCHY_PATH/shell" --tail 100`) shows no errors.
6. State folder contains only `state.json` + `lock`.
