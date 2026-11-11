import QtQuick
import qs.Commons
import qs.Ui

// Bar button for io.github.manish0467.btrfs-snapshots.
//
// Deliberately thin, like the arch-updates widget: it only draws the button
// and forwards the open/close contract to Panel.qml. The Panel owns the data
// (it runs the Python helper), so all real logic lives in helper/ and Panel.
//
// QML notes for readers coming from C++:
//   * `readonly property x: <expression>` is a *binding*: it re-evaluates
//     automatically whenever anything it reads changes.
//   * `panelLoader.item` is the instantiated Panel; it is null until loaded,
//     hence the `panelLoader.item ? ... : fallback` guards.
BarWidget {
  id: root
  moduleName: "io.github.manish0467.btrfs-snapshots"

  // ---- State shown on the button (all derived from the panel's model) -----
  readonly property string overall: panelLoader.item ? panelLoader.item.overall : "unknown"
  readonly property int total: panelLoader.item ? panelLoader.item.count : 0
  readonly property string age: panelLoader.item ? panelLoader.item.ageShort : ""
  // ESP at/above its usage limit: the widget must not look healthy, but it is
  // less urgent than lowdisk, so it shows as a degraded marker only.
  readonly property bool espDegraded: panelLoader.item ? panelLoader.item.espDegraded === true : false
  readonly property string idle: String(setting("idleBehavior", "dim"))      // dim | hide | show
  readonly property string countMode: String(setting("showCount", "both"))   // both | count | age | none

  // Nerd Font glyphs, one per state. Distinct shapes, not only colours, so
  // the states stay recognisable on themes with similar colours.
  readonly property var glyphs: ({
    "ok": "\uf058",           // check-circle
    "stale": "\uf017",        // clock (mild: stale is dimmed, never red)
    "lowdisk": "\uf071",      // warning triangle
    "degraded": "\uf071",     // warning triangle (ESP limit)
    "unavailable": "\uf05a",  // info-circle
    "unknown": "\uf021"       // rotate (before the first result arrives)
  })

  readonly property string glyph: {
    if (espDegraded && (root.overall === "ok" || root.overall === "stale"))
      return root.glyphs["degraded"]
    return root.glyphs[root.overall] || root.glyphs["unknown"]
  }

  // Label next to the glyph: "5·18h" (count and newest-snapshot age).
  // Hidden on vertical bars: the fixed-height slot cannot fit a text label
  // there (same convention as the media widget).
  readonly property string label: {
    if (!panelLoader.item || countMode === "none") return ""
    if (root.overall === "unknown" || root.overall === "unavailable") return ""
    if (root.vertical) return ""
    var parts = []
    if (countMode === "both" || countMode === "count") parts.push(String(root.total))
    if ((countMode === "both" || countMode === "age") && root.age !== "")
      parts.push(root.age)
    var espMark = root.espDegraded ? "!" : ""
    return parts.join("·") + espMark
  }

  // The label must widen the button: BarIconButton fixes its width to one
  // status slot and draws label + glyph from the slot's centre, so a label
  // wider than the slot bleeds over the neighbouring widgets. Measure the
  // label with a hidden Text using the very font BarIconButton's label uses
  // (it positions with label.implicitWidth too), and grow the slot to fit.
  // (FontMetrics cannot do this: it has no `text` property in Qt 6.)
  Text {
    id: labelMeasure
    visible: false
    textFormat: Text.PlainText
    text: root.label
    font.family: button.fontFamily
    font.pixelSize: Style.bar.iconFont
  }
  readonly property real labelExtra:
    root.label !== "" ? labelMeasure.implicitWidth + Style.space(4) : 0

  // Hidden only when everything is fine and the user asked for that. Stale,
  // low disk, ESP warning and unavailable keep the widget visible.
  visible: !(idle === "hide" && root.overall === "ok" && !root.espDegraded)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---- Contract with the shell (see develop page: BarWidget requirements) --
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }
  function refresh() { if (panelLoader.item) panelLoader.item.refresh(true) }

  // Hand the panel everything it needs from the bar. Re-run whenever one of
  // them changes (the bar and the settings arrive after construction).
  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyph
    labelText: root.label
    // Widen the fixed slot by the measured label so label + glyph stay
    // inside the button (see labelExtra above). Vertical bars keep the
    // default slot: their label is hidden anyway.
    slotSize: root.vertical ? Style.bar.statusSlot
                            : Style.bar.statusSlot + root.labelExtra
    // "Active" colours the icon with the urgent colour: lowdisk and
    // unavailable only. Stale stays mild (feedback E).
    active: root.overall === "lowdisk" || root.overall === "unavailable"
    useActiveColor: true
    // Fresh and quiet: fade the icon so the bar stays calm (never when
    // stale, degraded or worse - those states must stay noticeable).
    opacity: (root.overall === "ok" && !root.espDegraded && root.idle === "dim") ? 0.45 : 1.0
    tooltipText: panelLoader.item ? panelLoader.item.tooltip : "Btrfs Snapshots"

    // Left: panel. Right: refresh now. Middle: nothing.
    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else if (b === Qt.LeftButton) root.togglePanel()
    }
  }
}
