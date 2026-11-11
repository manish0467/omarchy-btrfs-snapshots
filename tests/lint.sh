#!/usr/bin/env bash
# Lint the QML with the *Qt 6* qmllint.
#
# Why this script exists:
#  * On Arch, /usr/bin/qmllint can be the old Qt 5 "syntax verifier", which
#    exits 0 even on broken files. The real linter is /usr/lib/qt6/bin/qmllint.
#  * Imports like `qs.Ui` are resolved by Quickshell, not by Qt, so qmllint
#    needs a directory that literally contains `qs/`. We build a throw-away one
#    that points at the shell's QML.
#
# Output: real problems are printed. Two known, harmless kinds are hidden
# because Omarchy's own plugins produce them too:
#   [unqualified]            ids used from nested components
#   "type QObject" members   Loader.item and untyped singletons (Style, Color)
# Exit status is 1 if anything else is reported.
set -euo pipefail
cd "$(dirname "$0")/.."

OMARCHY_PATH="${OMARCHY_PATH:-/usr/share/omarchy}"
QMLLINT="${QMLLINT:-/usr/lib/qt6/bin/qmllint}"
[[ -x $QMLLINT ]] || QMLLINT=$(command -v qmllint)

shim=$(mktemp -d)
trap 'rm -rf "$shim"' EXIT
ln -s "$OMARCHY_PATH/shell" "$shim/qs"

output=$("$QMLLINT" -I "$shim" BarWidget.qml Panel.qml 2>&1 || true)
problems=$(grep -E "^(Warning|Error)" <<<"$output" \
  | grep -v -E "\[unqualified\]|type \"QObject\"|QQuickLoader::item|on type QObject" || true)

if [[ -n $problems ]]; then
  echo "$problems"
  exit 1
fi
echo "qmllint: no unexpected problems ($("$QMLLINT" --version))"
