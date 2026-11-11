import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Details panel + data owner for io.github.manish0467.btrfs-snapshots.
//
// Responsibilities (and nothing else):
//   1. Run helper/btrfs_snapshots.py and keep its JSON output in `doc`.
//   2. Schedule refreshes (slow timer, limine.conf watcher, manual button).
//   3. Draw the panel; launch the confirmed snapshot creation.
// Parsing, classification and all state files live in the helper.
//
// PRIVILEGE NOTE: the only privileged operation this plugin ever triggers is
// Omarchy's own snapshot command, launched in the floating terminal where the
// user authenticates - see launchCreation() below. The command string is a
// compile-time constant; no user text ever enters a command. The plugin has
// no delete/restore/cleanup anywhere.
//
// QML notes for readers coming from C++:
//   * `property x: expr` is a binding; Qt re-evaluates it when inputs change.
//   * `Process`/`Timer`/`FileView` are child objects; their callbacks
//     (`onExited`, `onTriggered`, ...) are signal handlers, like connect().
//   * Every Text here is `Text.PlainText`: snapshot descriptions are untrusted
//     and must never be interpreted as markup.
Panel {
  id: root
  moduleName: "io.github.manish0467.btrfs-snapshots"
  manageIpc: false

  // Injected by BarWidget.injectPanel()
  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  // ------------------------------------------------------------------ model
  readonly property var emptyDoc: ({
    state: "unknown", checked_at: null, busy: false, errors: [], warnings: [],
    tiers: { limine: { status: "unknown", path: null }, snapper_full: "unknown" },
    configs: [], snapshots: [],
    counts: { total: 0, boot_entries: 0, configs: 0 },
    newest: null, fs: null, esp: null,
    show_config_column: false,
    creation: { command: "", retention_lines: [] }
  })
  property var doc: emptyDoc
  property bool busy: false
  property bool gotDoc: false
  property string runError: ""        // helper failed to run at all
  property bool confirmOpen: false    // creation confirmation dialog
  property string filterText: ""
  property int cursor: -1             // index into `rows`, -1 = none

  // Overall state for the bar button. If the helper cannot run and we have
  // nothing yet, that is an error too, never a blank.
  readonly property string overall: (runError !== "" && doc.state === "unknown") ? "error" : String(doc.state || "unknown")
  readonly property int count: doc.counts ? doc.counts.total : 0
  readonly property bool espDegraded: {
    var warns = doc.warnings || []
    for (var i = 0; i < warns.length; i++) if (warns[i].kind === "esp_full") return true
    return false
  }
  readonly property string ageShort: {
    var n = doc.newest
    if (!n || n.age_seconds === null || n.age_seconds === undefined) return ""
    return shortAge(n.age_seconds)
  }

  function shortAge(seconds) {
    var s = Math.max(0, Math.floor(seconds))
    if (s < 90) return "now"
    if (s < 5400) return Math.round(s / 60) + "m"
    if (s < 129600) return Math.round(s / 3600) + "h"
    return Math.round(s / 86400) + "d"
  }

  function humanSize(bytes) {
    var gb = 1024 * 1024 * 1024
    var mb = 1024 * 1024
    if (bytes >= gb * 10) return (Math.round(bytes / gb * 10) / 10) + " GiB"
    if (bytes >= gb) return (Math.round(bytes / gb * 100) / 100) + " GiB"
    if (bytes >= mb * 10) return (Math.round(bytes / mb * 10) / 10) + " MiB"
    return (Math.round(bytes / mb * 100) / 100) + " MiB"
  }

  // ------------------------------------------------------------- settings
  // Every option has a default here AND in the helper; see README for the table.
  readonly property string mockName: String(setting("mock", ""))
  readonly property int pollMinutes: Math.max(10, Number(setting("pollMinutes", 30)) || 30)
  readonly property string configJson: JSON.stringify({
    staleDays: setting("staleDays", 7),
    diskWarnPercent: setting("diskWarnPercent", 90),
    espWarnPercent: setting("espWarnPercent", 0)   // 0 = follow the sync tool's limit
  })

  // file:///home/me/.config/.../helper/btrfs_snapshots.py -> /home/me/.config/...
  readonly property string helperPath: decodeURIComponent(String(Qt.resolvedUrl("helper/btrfs_snapshots.py")).replace(/^file:\/\//, ""))

  // `python3 -I` = isolated mode: ignores PYTHON* env vars and the cwd, so
  // nothing but the stdlib and our script can be imported.
  function baseCommand() {
    var cmd = ["python3", "-I", helperPath, "--config", configJson]
    if (mockName !== "") cmd = cmd.concat(["--mock", mockName])
    return cmd
  }

  // ------------------------------------------------------------ open/close
  function open() { root.controller.show() }
  function close() {
    confirmOpen = false
    root.controller.hide()
  }
  function toggle() { root.opened ? close() : open() }
  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function") return bar.switchPanelFrom(barIdentity, direction)
    return false
  }

  // --------------------------------------------------------------- helper
  // Refresh = run `check`. Guard: only one check at a time per widget; the
  // helper additionally takes a file lock so several bars or a shell restart
  // can never overlap either.
  function refresh(force) {
    if (checkProc.running) return
    var args = ["check"]
    if (force) args.push("--force")
    checkProc.command = baseCommand().concat(args)
    gotDoc = false
    busy = true
    killTimer.restart()
    checkProc.running = true
  }

  function accept(text) {
    var parsed
    try { parsed = JSON.parse(text) } catch (e) {
      runError = "The helper returned invalid output"
      return
    }
    if (!parsed || parsed.schema !== 1) {
      runError = "The helper speaks an unsupported schema"
      return
    }
    gotDoc = true
    runError = ""
    doc = parsed
    if (parsed.busy) busyRetry.restart()   // another instance is checking; re-read soon
  }

  Process {
    id: checkProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.accept(text)
    }
    stderr: StdioCollector { id: checkErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.busy = false
      killTimer.stop()
      if (exitCode !== 0 && !root.gotDoc) {
        var lines = String(checkErr.text).trim().split("\n")
        root.runError = "Helper failed (exit " + exitCode + "): " + lines[lines.length - 1]
      }
    }
  }

  // Safety net: the helper has its own timeouts (~5 s per subprocess); if it
  // still hangs, kill it so the next refresh can run. Never blocks the shell.
  Timer {
    id: killTimer
    interval: 30000
    onTriggered: {
      checkProc.running = false
      root.busy = false
      root.runError = "The check timed out"
    }
  }

  // Slow poll. Snapshots change rarely; while the panel is closed 30 minutes
  // (floor 10) is plenty. The limine.conf watcher below is the fast path.
  Timer {
    interval: root.pollMinutes * 60000
    running: true
    repeat: true
    onTriggered: root.refresh(false)
  }

  // First check. Settings are injected by the bar *after* this object is
  // built, so wait a moment instead of reading them too early.
  Timer {
    id: startupTimer
    interval: 2000
    running: true
    onTriggered: root.refresh(false)
  }

  // Settings edited in shell.json: re-check (helper notices the changed config).
  property bool settingsSeen: false
  onSettingsChanged: {
    if (!settingsSeen) { settingsSeen = true; return }
    settingsDebounce.restart()
  }
  Timer {
    id: settingsDebounce
    interval: 800
    onTriggered: root.refresh(false)
  }

  Timer {
    id: busyRetry
    interval: 15000
    onTriggered: root.refresh(false)
  }

  // Event-driven fast path: limine-snapper-sync rewrites limine.conf whenever
  // a snapshot is created or removed. FileView watches it with inotify (no
  // polling). After the file has been quiet for 10 s, re-check. If the file
  // does not exist (no limine-sync), the watcher simply never fires.
  FileView {
    path: (doc && doc.tiers && doc.tiers.limine && doc.tiers.limine.path) ? doc.tiers.limine.path : "/boot/limine.conf"
    watchChanges: root.mockName === ""
    preload: false
    printErrors: false
    onFileChanged: limineQuiet.restart()
  }
  Timer {
    id: limineQuiet
    interval: 10000
    onTriggered: root.refresh(false)
  }

  // --------------------------------------------------------------- actions
  // Snapshot creation, the only state-changing thing this plugin does.
  // Exactly Omarchy's own command, as a compile-time constant (feedback Q1):
  // the floating terminal asks for authentication itself; the widget never
  // elevates and never builds command strings from user input.
  readonly property string creationCommand: doc.creation && doc.creation.command
                                            ? doc.creation.command : ""

  function startCreation() {
    if (confirmOpen) return
    confirmOpen = true   // the dialog shows the exact command; Cancel is default
  }

  function launchCreation() {
    confirmOpen = false
    if (bar && creationCommand !== "") {
      bar.run(creationCommand)
      // The terminal detaches immediately, so completion is detected via the
      // limine.conf watcher; these two one-shot refreshes are the fallback
      // (e.g. when limine-sync is not installed).
      refreshSoon2.restart()
      refreshSoon5.restart()
    }
    close()
  }
  Timer { id: refreshSoon2; interval: 2 * 60000; onTriggered: root.refresh(false) }
  Timer { id: refreshSoon5; interval: 5 * 60000; onTriggered: root.refresh(false) }

  // -------------------------------------------------------------- display
  function tierLine() {
    var limine = doc.tiers && doc.tiers.limine ? doc.tiers.limine.status : "unknown"
    var snapperFull = doc.tiers ? doc.tiers.snapper_full : "unknown"
    if (snapperFull === "ok") return "Full snapshot list (via Snapper)"
    if (limine === "ok") return "Boot-menu snapshots (world-readable boot config)"
    if (limine === "empty") return "Boot-menu data present, no snapshot entries"
    if (snapperFull === "denied")
      return "Snapper does not allow this user to list snapshots"
    return "No readable snapshot source"
  }

  function errorFor(source) {
    var errs = doc.errors || []
    for (var i = 0; i < errs.length; i++) if (errs[i].source === source) return errs[i].message
    return ""
  }

  function fsLine(fs) {
    if (!fs) return "unavailable"
    return humanSize(fs.used) + " used · " + humanSize(fs.avail) + " free · " + fs.percent_used + "%"
  }

  function ago(epoch) {
    if (!epoch) return "never"
    var s = Math.max(0, Date.now() / 1000 - epoch)
    if (s < 90) return "just now"
    if (s < 5400) return Math.round(s / 60) + " min ago"
    if (s < 129600) return Math.round(s / 3600) + " h ago"
    return Math.round(s / 86400) + " d ago"
  }

  readonly property string statusLine: {
    if (runError !== "" && doc.state === "unknown") return runError
    if (doc.state === "unknown") return busy ? "Checking…" : "Not checked yet"
    var parts = [tierLine(), "checked " + ago(doc.checked_at)]
    if (busy) parts = ["Checking…"]
    return parts.join(" · ")
  }

  readonly property string tooltip: {
    var lines = ["Btrfs Snapshots"]
    if (overall === "unknown") lines.push(busy ? "Checking…" : "Not checked yet")
    else if (overall === "error") lines.push("Error: " + runError)
    else if (overall === "unavailable")
      lines.push("No snapshot data readable: " + (errorFor("configs") || tierLine()))
    else {
      lines.push(count + " snapshot(s), newest " + (ageShort === "" ? "unknown age" : ageShort + " ago"))
      if (overall === "stale") lines.push("No recent snapshot (stale)")
      if (overall === "lowdisk") lines.push("Filesystem nearly full!")
      if (espDegraded) lines.push("Boot partition at/above its usage limit!")
    }
    lines.push("Click: details · Right-click: refresh")
    return lines.join("\n")
  }

  // The list is a flat array of row objects, rebuilt by a pure function so
  // the filter box and a fresh result both just re-evaluate this binding.
  readonly property var rows: buildRows(doc, filterText, runError)

  function matches(q, text) { return q === "" || String(text).toLowerCase().indexOf(q) >= 0 }

  function buildRows(d, filter, failure) {
    var q = String(filter || "").trim().toLowerCase()
    var out = []

    // ---- Filesystem summary
    out.push({ kind: "header", title: "FILESYSTEM" })
    out.push({ kind: "fs", label: "Root (btrfs)", value: fsLine(d.fs) })
    if (d.esp && d.esp.path) {
      var espWarn = d.esp.warn_percent ? " · limit " + d.esp.warn_percent + "%" : ""
      out.push({ kind: "fs", label: "Boot partition (" + d.esp.path + ")",
                 value: d.esp.percent_used + "%" + espWarn, warn: espDegraded })
    }
    // Sizes: quotas are off on stock Omarchy and per-snapshot sizes need
    // them (or expensive scans). Never lie and never scan: say it plainly.
    out.push({ kind: "note",
               text: "Snapshot sizes: unavailable (Btrfs quotas off; enabling them is a system change this plugin never makes).",
               warn: false })
    if (espDegraded) {
      var w = (d.warnings || []).filter(function(x) { return x.kind === "esp_full" })[0]
      out.push({ kind: "note", text: w ? w.message : "Boot partition at/above its usage limit.", warn: true })
    }

    // ---- Snapshots
    out.push({ kind: "header", title: "SNAPSHOTS  ·  " + (d.counts ? d.counts.total : 0) })
    var snaps = (d.snapshots || []).slice()
    var shown = 0
    for (var i = 0; i < snaps.length; i++) {
      var s = snaps[i]
      var hay = [s.description, s.type, s.cleanup, s.config, s.user,
                 s.number].join(" ")
      if (!matches(q, hay)) continue
      shown++
      out.push({ kind: "snap", snap: s })
    }
    if (shown === 0) {
      if (d.state === "unavailable") {
        var msg = "Snapshot listing is not available without extra permissions on this system."
        if (d.tiers && d.tiers.snapper_full === "denied")
          msg += " Snapper does not allow this user to list snapshots, and no boot-menu data was found. See the plugin README for an optional, manual setup you could choose to do yourself - the plugin never changes Snapper settings."
        out.push({ kind: "note", text: msg, warn: true })
      } else if (d.errors && d.errors.length > 0) {
        var first = d.errors[0]
        out.push({ kind: "note", text: (first.message || "check failed"), warn: true })
        if (first.kind === "missing" || first.kind === "no-configs")
          out.push({ kind: "note",
                     text: "Omarchy can set up Snapper for you - see \"Omarchy's own ways\" below and the plugin README.",
                     warn: false })
      } else {
        out.push({ kind: "note", text: q ? "No matching snapshots" : "No snapshots (yet)", warn: false })
      }
    } else if (d.tiers && d.tiers.snapper_full !== "ok") {
      out.push({ kind: "note",
                 text: "These are the boot-menu entries kept by limine-snapper-sync (a subset of all snapshots; types and cleanup algorithms are only visible with full Snapper access).",
                 warn: false })
    }

    // ---- Omarchy's own ways (feedback A: only commands without elevation)
    out.push({ kind: "header", title: "OMARCHY'S OWN WAYS" })
    out.push({ kind: "note", text: "Create a snapshot: the button below, or Omarchy's command menu (omarchy-snapshot create).", warn: false })
    out.push({ kind: "note", text: "Restore: omarchy-snapshot restore in Omarchy's command menu, or boot a snapshot entry from the Limine boot menu.", warn: false })
    out.push({ kind: "note", text: "Deleting snapshots, cleanup and boot-entry removal: see the plugin README (destructive commands live there, not in this plugin).", warn: false })

    if (failure !== "") out.unshift({ kind: "note", text: failure, warn: true })
    return out
  }

  // Move the keyboard cursor to the next selectable row.
  function moveCursor(step) {
    var n = rows.length
    if (n === 0) return
    var i = cursor
    for (var k = 0; k < n; k++) {
      i += step
      if (i < 0 || i >= n) return
      if (rows[i].kind === "snap") {
        cursor = i
        list.positionViewAtIndex(i, ListView.Contain)
        return
      }
    }
  }

  onRowsChanged: if (cursor >= rows.length) cursor = -1

  // ------------------------------------------------------------------- UI
  readonly property color fg: Commons.Color.popups.text
  readonly property color dim: Util.alpha(fg, 0.6)

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      implicitHeight: column.implicitHeight
      // While typing in the filter box, every key belongs to the text field.
      blocked: filterInput.activeFocus

      onCloseRequested: { if (root.confirmOpen) root.confirmOpen = false; else root.close() }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) {
        if (root.confirmOpen) confirm.selectedIndex = confirm.selectedIndex === 0 ? 1 : 0
        else if (dy !== 0) root.moveCursor(dy)
      }
      onActivateRequested: {
        if (root.confirmOpen) {
          if (confirm.selectedIndex === 0) root.confirmOpen = false
          else root.launchCreation()
          return
        }
        // snapshot rows are informational; nothing to activate in v1
      }
      onTextKey: function(t, modifiers) {
        if (root.confirmOpen) return
        if (t === "/") filterInput.forceActiveFocus()
        else if (t === "r") root.refresh(true)
        else if (t === "c") root.startCreation()
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(8)

        // ---- Title row: status + refresh
        Item {
          width: parent.width
          height: Math.max(titleText.implicitHeight, refreshButton.height)

          Column {
            anchors.left: parent.left
            anchors.right: refreshButton.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            Text {
              id: titleText
              text: "Btrfs Snapshots"
              textFormat: Text.PlainText
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            Text {
              width: parent.width
              text: root.statusLine
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
          PanelActionButton {
            id: refreshButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: ""
            tooltipText: "Refresh now (r)"
            enabled: !root.busy
            opacity: root.busy ? 0.5 : 1.0
            onClicked: root.refresh(true)
          }
        }

        // ---- Filter box (press / to focus, Escape to leave)
        Rectangle {
          width: parent.width
          height: filterInput.implicitHeight + Style.space(10)
          radius: Style.cornerRadius
          color: Util.alpha(root.fg, 0.07)

          FilterInput {
            id: filterInput
            anchors.fill: parent
            anchors.leftMargin: Style.space(8)
            anchors.rightMargin: Style.space(8)
            verticalAlignment: TextInput.AlignVCenter
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            focus: false
            onTextChanged: root.filterText = text
            // First Escape clears the text, the second hands the keys back.
            Keys.onEscapePressed: function(event) {
              if (text !== "") text = ""
              else keys.forceActiveFocus()
              event.accepted = true
            }
          }
          Text {
            anchors.left: parent.left
            anchors.leftMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            visible: filterInput.text === "" && !filterInput.activeFocus
            text: "Filter snapshots  ( / )"
            textFormat: Text.PlainText
            color: root.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }
        }

        // ---- Sections in one scrolling list
        ListView {
          id: list
          width: parent.width
          height: Math.min(contentHeight, Style.space(420))
          clip: true
          model: root.rows
          boundsBehavior: Flickable.StopAtBounds
          spacing: Style.space(2)
          reuseItems: false

          delegate: Item {
            id: row
            required property var modelData
            required property int index
            width: ListView.view.width
            readonly property bool selected: index === root.cursor
            readonly property bool isSnap: modelData.kind === "snap"
            height: isSnap ? snapBox.implicitHeight + Style.space(10)
                  : modelData.kind === "header" ? Style.space(26)
                  : modelData.kind === "fs" ? Style.space(24)
                  : noteText.implicitHeight + Style.space(6)

            Rectangle {
              anchors.fill: parent
              radius: Style.cornerRadius
              visible: row.selected
              color: Util.alpha(Commons.Color.accent, 0.15)
            }

            // header
            Text {
              visible: row.modelData.kind === "header"
              anchors.left: parent.left
              anchors.leftMargin: Style.space(4)
              anchors.bottom: parent.bottom
              anchors.bottomMargin: Style.space(3)
              text: row.modelData.title || ""
              textFormat: Text.PlainText
              color: Qt.darker(root.fg, 1.4)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            // filesystem summary row: label .... value
            Item {
              visible: row.modelData.kind === "fs"
              anchors.fill: parent
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)

              Text {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width * 0.5
                elide: Text.ElideRight
                text: row.modelData.label || ""
                textFormat: Text.PlainText
                color: root.fg
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                horizontalAlignment: Text.AlignRight
                elide: Text.ElideLeft
                text: row.modelData.value || ""
                textFormat: Text.PlainText
                color: row.modelData.warn ? Commons.Color.urgent : root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
            }

            // note (empty / error / info)
            Text {
              id: noteText
              visible: row.modelData.kind === "note"
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)
              wrapMode: Text.Wrap
              text: row.modelData.text || ""
              textFormat: Text.PlainText
              color: row.modelData.warn ? Commons.Color.urgent : root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            // snapshot row: #num  date  description  [badges]
            Item {
              visible: row.isSnap
              anchors.fill: parent
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(4)

              Column {
                id: snapBox
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(1)

                Text {
                  width: parent.width
                  elide: Text.ElideRight
                  // Untrusted description -> plain text, never markup.
                  text: {
                    var s = row.modelData.snap
                    var head = "#" + s.number
                    if (s.description) head += "  " + s.description
                    return head
                  }
                  textFormat: Text.PlainText
                  color: root.fg
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
                Text {
                  width: parent.width
                  elide: Text.ElideRight
                  text: {
                    var s = row.modelData.snap
                    var when = s.date_epoch ? Qt.formatDateTime(new Date(s.date_epoch * 1000), "d MMM yyyy, hh:mm")
                                            : (s.date_raw || "date unknown")
                    var badges = []
                    if (s.type) badges.push(s.type)
                    if (s.cleanup) badges.push("cleanup: " + s.cleanup)
                    if (s.config) badges.push("config: " + s.config)
                    if (s.boot_entry) badges.push("in boot menu")
                    if (s.user) badges.push("by " + s.user)
                    return when + (badges.length ? "  ·  " + badges.join(" · ") : "")
                  }
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }
            }

            MouseArea {
              anchors.fill: parent
              z: -1
              hoverEnabled: true
              onEntered: root.cursor = row.index
            }
          }
        }

        // ---- Footer: the one state-changing action
        Button {
          id: createButton
          width: parent.width
          text: "Create snapshot…"
          iconText: ""
          bordered: true
          active: true
          // Offered whenever Omarchy's command exists (it decides everything
          // else in its own terminal); disabled while the helper is running
          // so the dialog never races a refresh.
          enabled: root.overall !== "error" && !root.busy
          opacity: enabled ? 1.0 : 0.5
          onClicked: root.startCreation()
        }
      }

      // Confirmation before anything runs. Shows the EXACT command and the
      // retention warning (feedback B); Cancel is the default choice.
      ConfirmDialog {
        id: confirm
        anchors.fill: parent
        z: 10
        opened: root.confirmOpen
        selectedIndex: 0
        onOpenedChanged: if (opened) selectedIndex = 0
        message: {
          var lines = []
          if (root.creationCommand !== "") {
            lines.push("Run this command?")
            lines.push(root.creationCommand)
          } else {
            lines.push("Snapshot creation command unavailable.")
          }
          var ret = (root.doc.creation && root.doc.creation.retention_lines) || []
          if (ret.length > 0) {
            lines.push("")
            for (var i = 0; i < ret.length; i++) lines.push(ret[i])
          }
          lines.push("")
          lines.push("The command runs in a terminal where you authenticate; the plugin itself never elevates.")
          return lines.join("\n")
        }
        cancelText: "Cancel"
        confirmText: "Create snapshot"
        background: Commons.Color.popups.background
        foreground: root.fg
        onCanceled: root.confirmOpen = false
        onConfirmed: root.launchCreation()
      }
    }
  }
}
