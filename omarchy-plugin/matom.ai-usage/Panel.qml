import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// AI Usage — Omarchy shell front end for the ai-usage-indicator backend.
//
// Pure presentation, like the GNOME extension next door: it reads the
// state.json the Python service writes and renders each provider inline as
// [icon][meter][percent], with a details panel on click. It never calls a
// provider API itself. Colors come from the active Omarchy theme: meters fill
// in the bar foreground and only turn toward `urgent` as a plan runs out.
Panel {
  id: root
  moduleName: "matom.ai-usage"
  ipcTarget: "matom.ai-usage"
  manageIpc: false

  readonly property string home: Quickshell.env("HOME")
  readonly property string cacheHome: {
    var xdg = Quickshell.env("XDG_CACHE_HOME")
    return xdg && xdg !== "" ? xdg : home + "/.cache"
  }
  readonly property string statePath: {
    var custom = String(setting("statePath", ""))
    if (custom === "") return cacheHome + "/ai-usage-indicator/state.json"
    return custom.indexOf("~/") === 0 ? home + custom.slice(1) : custom
  }
  readonly property string refreshCommand: {
    var custom = String(setting("refreshCommand", ""))
    return custom !== "" ? custom : Util.shellQuote(home + "/.local/bin/ai-usage-indicator") + " --once"
  }
  readonly property bool showMeters: setting("showMeters", true) === true
  readonly property bool brandColors: setting("brandColors", false) === true
  readonly property int staleAfterSec: Number(setting("staleAfterSec", 900)) || 900

  // Bar chips follow the bar's own foreground; the panel follows the popup's.
  readonly property color chipForeground: root.barForeground
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property bool vertical: bar ? bar.vertical : false

  // Not `state`: that name is Item's own state-machine property.
  property var usageState: null
  property bool refreshing: false
  property double nowSec: Date.now() / 1000

  readonly property var providers: usageState && usageState.providers ? usageState.providers : []
  readonly property double updatedAt: usageState && usageState.updated_at ? Number(usageState.updated_at) : 0
  readonly property bool stale: updatedAt > 0 && nowSec - updatedAt > staleAfterSec
  readonly property bool alarming: {
    for (var i = 0; i < providers.length; i++)
      if (providers[i].pressure === "near-limit") return true
    return false
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }
  function hexColor(c) {
    function channel(v) { return ("0" + Math.round(root.clamp(v, 0, 1) * 255).toString(16)).slice(-2) }
    return "#" + channel(c.r) + channel(c.g) + channel(c.b)
  }

  function hasPercent(p) {
    return !!p && p.percent !== null && p.percent !== undefined && isFinite(Number(p.percent))
  }

  // Theme-derived pressure ramp: plain foreground while there's room, a
  // half-step toward urgent at the warning line, full urgent near the limit.
  function pressureColor(p, base) {
    if (!p || p.error) return root.urgent
    if (p.pressure === "near-limit") return root.urgent
    if (p.pressure === "warning") return Qt.tint(base, root.alpha(root.urgent, 0.55))
    if (p.pressure === "unknown") return Qt.darker(base, 1.55)
    return base
  }

  // Percent when the plan API reports one; otherwise the first piece of the
  // compact label (e.g. "$0.44" for Grok's local spend). Mirrors the GNOME chip.
  function glanceText(p) {
    if (!p) return ""
    if (p.error) return "!"
    if (hasPercent(p)) return Math.round(Number(p.percent)) + "%"
    var compact = String(p.label || "").split(" · ")[0].trim()
    return compact !== "" ? compact : "—"
  }

  function agoText() {
    if (!(updatedAt > 0)) return "Never updated"
    var secs = Math.max(0, Math.round(nowSec - updatedAt))
    if (secs < 60) return "Updated " + secs + "s ago"
    if (secs < 3600) return "Updated " + Math.round(secs / 60) + "m ago"
    if (secs < 86400) return "Updated " + Math.round(secs / 3600) + "h ago"
    return "Updated " + Math.round(secs / 86400) + "d ago"
  }

  function tooltipText() {
    if (providers.length === 0) return "AI usage — backend not running"
    var parts = []
    for (var i = 0; i < providers.length; i++)
      parts.push(providers[i].display_name + " " + glanceText(providers[i]))
    return parts.join(" · ") + (stale ? "  (stale)" : "")
  }

  function readState() {
    try {
      var text = stateFile.text()
      usageState = text && text.trim() !== "" ? JSON.parse(text) : null
    } catch (e) {
      // A half-read file during a write is the likely cause; the backend
      // writes atomically, so the next change or poll will land a good copy.
    }
  }

  function refreshNow() {
    if (!root.bar) return
    root.refreshing = true
    refreshTimeout.restart()
    root.bar.run(root.refreshCommand)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    nowSec = Date.now() / 1000
    stateFile.reload()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      root.readState()
      root.refreshing = false
    }
    onLoadFailed: root.usageState = null
  }

  // Fallback poll (an atomic rename can slip past a watch) and the clock for
  // the "updated ago" line and stale dimming.
  Timer {
    interval: 15000
    running: true
    repeat: true
    onTriggered: {
      root.nowSec = Date.now() / 1000
      stateFile.reload()
    }
  }

  Timer {
    id: refreshTimeout
    interval: 90000
    onTriggered: root.refreshing = false
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refreshNow(); return "ok" }
  }

  // ---------------------------------------------------------------- bar

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    dimmed: root.stale
    tooltipText: root.tooltipText()
    fixedWidth: root.vertical ? -1 : chips.implicitWidth + scaledHorizontalMargin * 2
    fixedHeight: root.vertical ? chips.implicitHeight + scaledVerticalPadding * 2 : -1

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton || buttonCode === Qt.RightButton) root.refreshNow()
      else root.toggle()
    }

    Grid {
      id: chips
      anchors.centerIn: parent
      columns: root.vertical ? 1 : Math.max(1, root.providers.length)
      columnSpacing: Style.space(10)
      rowSpacing: Style.space(6)

      // No state yet: a single placeholder, so the widget stays clickable and
      // the panel can say why it is empty.
      Text {
        visible: root.providers.length === 0
        textFormat: Text.PlainText
        text: root.vertical ? "AI" : "AI …"
        color: root.alpha(root.chipForeground, 0.6)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }

      Repeater {
        model: root.providers

        Chip {
          required property var modelData
          provider: modelData
        }
      }
    }
  }

  // [icon][meter][percent] horizontally; icon over percent on a vertical bar.
  component Chip: Grid {
    id: chip
    property var provider: null
    readonly property color tone: root.pressureColor(provider, root.chipForeground)

    columns: root.vertical ? 1 : 3
    spacing: Style.space(root.vertical ? 2 : 5)
    horizontalItemAlignment: Grid.AlignHCenter
    verticalItemAlignment: Grid.AlignVCenter

    ProviderIcon {
      providerId: chip.provider ? chip.provider.id : ""
      name: chip.provider ? chip.provider.display_name : ""
      size: Style.bar.iconCanvas - 2
      tint: root.chipForeground
    }

    Meter {
      visible: root.showMeters && !root.vertical
      width: Style.space(34)
      thickness: Style.space(5)
      value: root.hasPercent(chip.provider) ? Number(chip.provider.percent) / 100 : -1
      fill: chip.tone
      trackColor: root.alpha(root.chipForeground, 0.18)
    }

    Text {
      textFormat: Text.PlainText
      text: root.glanceText(chip.provider)
      color: chip.provider && (chip.provider.error || chip.provider.pressure !== "normal")
        ? chip.tone : root.chipForeground
      font.family: root.fontFamily
      font.pixelSize: root.vertical ? Style.font.caption : Style.font.body
      font.features: { "tnum": 1 }
    }
  }

  // ---------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(56), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onActivateRequested: root.refreshNow()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refreshNow() }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "AI Usage"
            meta: root.refreshing ? "Refreshing…" : root.agoText()
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: "󱚣"
                color: root.alarming ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          Text {
            visible: root.providers.length === 0
            width: parent.width
            topPadding: Style.space(8)
            textFormat: Text.PlainText
            text: "No usage data yet.\nStart the backend with\nsystemctl --user start ai-usage-indicator"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          PanelSeparator {
            visible: root.providers.length > 0
            foreground: root.foreground
          }

          PanelSectionHeader {
            visible: root.providers.length > 0
            text: "PLANS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.providers

            ProviderRow {
              required property var modelData
              width: column.width
              provider: modelData
            }
          }

          PanelSeparator { foreground: root.foreground }

          Button {
            width: parent.width
            text: root.refreshing ? "Refreshing…" : "Refresh now"
            iconText: "󰑐"
            iconSpinning: root.refreshing
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            verticalPadding: Style.spacing.controlPaddingY
            onClicked: root.refreshNow()
          }
        }
      }
    }
  }

  // Name and value, meter, then the breakdown and reset time underneath.
  component ProviderRow: Column {
    id: row
    property var provider: null
    readonly property color tone: root.pressureColor(provider, root.foreground)

    spacing: Style.space(6)

    Item {
      width: parent.width
      implicitHeight: Math.max(rowIcon.height, rowName.implicitHeight, rowValue.implicitHeight)

      ProviderIcon {
        id: rowIcon
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        providerId: row.provider ? row.provider.id : ""
        name: row.provider ? row.provider.display_name : ""
        size: Style.font.iconLarge
        tint: root.foreground
      }

      Text {
        id: rowName
        textFormat: Text.PlainText
        text: row.provider ? row.provider.display_name : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        elide: Text.ElideRight
        anchors.left: rowIcon.right
        anchors.leftMargin: Style.space(8)
        anchors.right: rowValue.left
        anchors.rightMargin: Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: rowValue
        textFormat: Text.PlainText
        text: root.glanceText(row.provider)
        color: row.provider && (row.provider.error || row.provider.pressure !== "normal")
          ? row.tone : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.features: { "tnum": 1 }
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    Meter {
      width: parent.width
      value: root.hasPercent(row.provider) ? Number(row.provider.percent) / 100 : -1
      fill: row.tone
      trackColor: root.track
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      visible: text !== ""
      text: row.provider ? String(row.provider.error || row.provider.label || "") : ""
      color: row.provider && row.provider.error ? root.urgent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      visible: text !== ""
      text: row.provider && row.provider.reset_text ? String(row.provider.reset_text) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  // Rounded track filled to `value` (0–1); an unknown value leaves it empty.
  component Meter: Item {
    id: meter
    property real value: -1
    property color fill: root.foreground
    property color trackColor: root.track
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))

    implicitHeight: thickness
    height: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: meter.trackColor
    }

    Rectangle {
      visible: meter.value > 0
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meterTrack.height
      radius: meterTrack.radius
      width: Math.max(height, meterTrack.width * root.clamp(meter.value, 0, 1))
      color: meter.fill

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }
  }

  // assets/<id>.svg. Themed (the default), the SVG's own colors are swapped
  // for the theme foreground and the result loaded as a data URL, so the
  // mark is redrawn in the theme color at full sharpness. brandColors shows
  // the file as-is. A missing icon falls back to the provider's initial.
  component ProviderIcon: Item {
    id: icon
    property string providerId: ""
    property string name: ""
    property real size: Style.bar.iconCanvas
    property color tint: root.foreground

    readonly property string fileUrl: providerId !== "" ? String(Qt.resolvedUrl("assets/" + providerId + ".svg")) : ""
    readonly property string svgText: svgFile.loaded ? svgFile.text() : ""
    readonly property bool ready: iconImage.status === Image.Ready

    width: size
    height: size

    FileView {
      id: svgFile
      property bool loaded: false
      path: icon.fileUrl.indexOf("file://") === 0 ? decodeURIComponent(icon.fileUrl.slice(7)) : ""
      printErrors: false
      onLoaded: loaded = true
      onLoadFailed: loaded = false
      onPathChanged: loaded = false
    }

    Image {
      id: iconImage
      anchors.fill: parent
      source: {
        if (root.brandColors || icon.svgText === "") return icon.fileUrl
        var svg = icon.svgText.replace(/#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b/g, root.hexColor(icon.tint))
        return "data:image/svg+xml;utf8," + encodeURIComponent(svg)
      }
      sourceSize.width: Math.ceil(icon.size * Screen.devicePixelRatio)
      sourceSize.height: Math.ceil(icon.size * Screen.devicePixelRatio)
      fillMode: Image.PreserveAspectFit
      smooth: true
      visible: icon.ready
    }

    Text {
      anchors.centerIn: parent
      visible: !icon.ready
      textFormat: Text.PlainText
      text: icon.name !== "" ? icon.name.charAt(0).toUpperCase() : "?"
      color: icon.tint
      font.family: root.fontFamily
      font.pixelSize: Math.round(icon.size * 0.8)
      font.bold: true
    }
  }
}
