import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.bjarkimg.android-tv-remote"

  property bool popupOpen: false
  readonly property bool opened: popupOpen
  property bool online: false
  property bool sessionReady: false
  property bool reconnecting: false
  property bool scanning: false
  property string statusText: "READY"
  property string processError: ""
  property string viewMode: "remote"
  property string activeDeviceName: deviceName
  property string activeIdentifier: ""
  property string activeHost: host
  property string pairingName: ""
  property string pairingIdentifier: ""
  property var devices: []
  property var actionQueue: []
  property int selectedDeviceIndex: 0
  property string nowPlaying: ""
  property string currentApp: ""
  property string powerState: "unknown"
  property int volumeLevel: -1
  property int volumeMax: 0
  property bool tvMuted: false
  property string hoverHint: ""

  readonly property string playingLine: {
    if (!root.online) return "OFFLINE"
    if (root.powerState === "asleep") return "ASLEEP"
    if (root.nowPlaying !== "") return "PLAYING  ·  " + root.nowPlaying.toUpperCase()
    return "PLAYING  ·  —"
  }
  readonly property string volumeLine: {
    if (root.tvMuted) return "MUTED"
    if (root.volumeMax > 0 && root.volumeLevel >= 0) return "VOL " + String(root.volumeLevel)
    return ""
  }

  readonly property string deviceName: String(setting("deviceName", "Android TV"))
  readonly property string host: String(setting("host", ""))
  readonly property string remotePath: decodeURIComponent(
    String(Qt.resolvedUrl("android-tv-remote")).replace(/^file:\/\//, "")
  )
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.58)
  readonly property color accent: bar ? bar.urgent : Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : "JetBrainsMono Nerd Font"

  function close() {
    if (viewMode === "pin") sendRequest({ "op": "pair-cancel" })
    popupOpen = false
    viewMode = "remote"
    actionQueue = []
    hoverHint = ""
  }

  function setHoverHint(text, hovering) {
    var value = String(text || "")
    if (hovering) hoverHint = value
    else if (hoverHint === value) hoverHint = ""
  }

  function open() {
    popupOpen = true
  }

  function actionLabel(action) {
    var value = String(action || "")
    if (value.indexOf("app-") === 0) return value.slice(4).toUpperCase()
    if (value === "ff") return "FAST FORWARD"
    if (value === "power-off") return "POWER OFF"
    if (value === "toggle-power") return "POWER"
    return value.toUpperCase().replace(/-/g, " ")
  }

  function actionTooltip(action) {
    switch (String(action || "")) {
      case "back": return "Back  [B]"
      case "home": return "Home screen  [G]"
      case "menu": return "Menu / options  [M]"
      case "up": return "Up  [K]"
      case "down": return "Down  [J]"
      case "left": return "Left  [H]"
      case "right": return "Right  [L]"
      case "select": return "Select / OK  [Enter]"
      case "volume-down": return "Volume down  [-]"
      case "volume-up": return "Volume up  [+]"
      case "play-pause": return "Play / pause  [P]"
      case "previous": return "Previous track or chapter  ["
      case "next": return "Next track or chapter  ]"
      case "rewind": return "Rewind  [R]"
      case "ff": return "Fast forward  [F]"
      case "mute": return "Mute  [X]"
      case "wake": return "Wake the TV  [W]"
      case "power-off": return "Sleep / power off  [S]"
      case "app-plex": return "Open Plex  [1]"
      case "app-netflix": return "Open Netflix  [2]"
      case "app-youtube": return "Open YouTube  [3]"
      case "app-disney": return "Open Disney+  [4]"
      case "app-prime": return "Open Prime Video  [5]"
      case "app-settings": return "Open TV settings  [6]"
      default: return actionLabel(action)
    }
  }

  function powerStatusLabel(status) {
    var value = String(status || "")
    if (value === "awake") return "ONLINE · AWAKE"
    if (value === "asleep") return "ONLINE · ASLEEP"
    if (String(value).indexOf(".On") >= 0) return "ONLINE · AWAKE"
    return value ? "ONLINE" : "ONLINE"
  }

  function sendAction(action) {
    if (!action) return
    if (sessionProcess.running && sessionReady) {
      statusText = actionLabel(action)
      sessionProcess.write(action + "\n")
      return
    }
    if (actionQueue.length < 64) actionQueue = actionQueue.concat([action])
  }

  function sendRequest(request) {
    if (!sessionProcess.running) return false
    sessionProcess.write(JSON.stringify(request) + "\n")
    return true
  }

  function flushQueuedActions() {
    if (!sessionProcess.running || !sessionReady || actionQueue.length === 0) return
    var pending = actionQueue
    actionQueue = []
    for (var index = 0; index < pending.length; index++) {
      sessionProcess.write(pending[index] + "\n")
    }
  }

  function updateActiveDevice(message) {
    activeDeviceName = String(message.name || activeDeviceName)
    activeIdentifier = String(message.identifier || activeIdentifier)
    activeHost = String(message.host || activeHost)
  }

  function applyPlayback(message) {
    if (message.appLabel !== undefined) nowPlaying = String(message.appLabel || "")
    if (message.app !== undefined) currentApp = String(message.app || "")
    if (message.status) powerState = String(message.status)
    if (message.volume !== undefined && message.volume !== null && message.volume !== "") {
      volumeLevel = Number(message.volume)
    }
    if (message.volumeMax !== undefined && message.volumeMax !== null && message.volumeMax !== "") {
      volumeMax = Number(message.volumeMax)
    }
    if (message.muted !== undefined) tvMuted = Boolean(message.muted)
  }

  function sendSearch() {
    var value = String(searchInput.text || "").trim()
    if (!value) {
      processError = "Enter text to send to the TV"
      return
    }
    processError = ""
    statusText = "SENDING"
    sendRequest({ "op": "text", "value": value })
    searchInput.text = ""
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function openDevices() {
    viewMode = "devices"
    processError = ""
    scanDevices()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function scanDevices() {
    processError = ""
    if (!sessionProcess.running) {
      manualReconnect()
    }
    if (sendRequest({ "op": "discover" })) {
      scanning = true
      statusText = "SCANNING"
      scanTimeout.restart()
    } else {
      scanning = false
      scanTimeout.stop()
      statusText = "OFFLINE"
      processError = "Remote session is not running"
    }
  }

  function backToRemote() {
    if (viewMode === "pin") sendRequest({ "op": "pair-cancel" })
    viewMode = "remote"
    processError = ""
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function startPairing(identifier, name) {
    pairingIdentifier = String(identifier || "")
    pairingName = String(name || "Android TV")
    processError = ""
    statusText = "STARTING PAIR"
    sendRequest({ "op": "pair-start", "identifier": pairingIdentifier })
  }

  function finishPairing(pin) {
    if (String(pin).length !== 6) {
      processError = "Enter all six characters"
      return
    }
    processError = ""
    statusText = "PAIRING"
    sendRequest({ "op": "pair-finish", "pin": String(pin).toUpperCase() })
  }

  function cancelPairing() {
    sendRequest({ "op": "pair-cancel" })
    viewMode = "devices"
    processError = ""
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function activateSelectedDevice() {
    if (selectedDeviceIndex < 0 || selectedDeviceIndex >= devices.length) return
    var device = devices[selectedDeviceIndex]
    if (device.paired) {
      statusText = "CONNECTING"
      sendRequest({ "op": "switch", "identifier": String(device.identifier) })
    } else {
      startPairing(device.identifier, device.name)
    }
  }

  function addHost() {
    var value = String(hostInput.text || "").trim()
    if (!value) {
      processError = "Enter a host as IP or hostname"
      return
    }
    processError = ""
    statusText = "ADDING"
    sendRequest({
      "op": "add",
      "host": value,
      "name": String(nameInput.text || "").trim()
    })
  }

  function removeDevice(identifier) {
    var target = String(identifier || "")
    if (!target && selectedDeviceIndex >= 0 && selectedDeviceIndex < devices.length) {
      target = String(devices[selectedDeviceIndex].identifier || "")
    }
    if (!target) {
      processError = "No device selected"
      return
    }
    processError = ""
    statusText = "REMOVING"
    sendRequest({ "op": "remove", "identifier": target })
  }

  function removeSelectedDevice() {
    if (selectedDeviceIndex < 0 || selectedDeviceIndex >= devices.length) {
      processError = "No device selected"
      return
    }
    removeDevice(devices[selectedDeviceIndex].identifier)
  }

  function moveDeviceCursor(dy) {
    if (devices.length === 0 || dy === 0) return
    selectedDeviceIndex = Math.max(0, Math.min(devices.length - 1, selectedDeviceIndex + dy))
  }

  function handleSessionLine(line) {
    var message
    try {
      message = JSON.parse(String(line || ""))
    } catch (error) {
      return
    }

    if (message.event === "ready") {
      sessionReady = true
      reconnecting = false
      online = Boolean(message.connected)
      updateActiveDevice(message)
      applyPlayback(message)
      statusText = online ? powerStatusLabel(message.status) : "NO DEVICE"
      flushQueuedActions()
      return
    }

    if (message.event === "restarting") {
      sessionReady = false
      reconnecting = true
      online = false
      statusText = "RECONNECTING"
      actionQueue = [String(message.action || "")].concat(actionQueue)
      return
    }

    if (message.event === "removed") {
      var removedId = String(message.identifier || "")
      if (removedId === activeIdentifier || !message.connected) {
        sessionReady = false
        online = false
        activeDeviceName = deviceName
        activeIdentifier = ""
        activeHost = host
        nowPlaying = ""
        currentApp = ""
        powerState = "unknown"
        volumeLevel = -1
        volumeMax = 0
        tvMuted = false
        statusText = "REMOVED"
      }
      return
    }

    if (message.event === "devices") {
      scanning = false
      scanTimeout.stop()
      devices = message.devices || []
      selectedDeviceIndex = Math.max(0, Math.min(selectedDeviceIndex, Math.max(0, devices.length - 1)))
      for (var index = 0; index < devices.length; index++) {
        if (String(devices[index].identifier) === activeIdentifier) {
          selectedDeviceIndex = index
          break
        }
      }
      if (statusText !== "REMOVED") statusText = String(devices.length) + " FOUND"
      if (devices.length > 0) processError = ""
      return
    }

    if (message.event === "pairing-pin") {
      pairingIdentifier = String(message.identifier || pairingIdentifier)
      pairingName = String(message.name || pairingName)
      viewMode = "pin"
      statusText = "PIN REQUIRED"
      pinInput.text = ""
      Qt.callLater(function() { pinInput.forceActiveFocus() })
      return
    }

    if (message.event === "pairing-cancelled") {
      viewMode = "devices"
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
      return
    }

    if (message.event === "switched" || message.event === "paired") {
      updateActiveDevice(message)
      applyPlayback(message)
      sessionReady = true
      online = message.connected !== false
      viewMode = "remote"
      statusText = message.event === "paired" ? "PAIRED" : "ONLINE"
      processError = ""
      flushQueuedActions()
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
      return
    }

    if (message.event === "now-playing") {
      applyPlayback(message)
      if (message.connected !== undefined) online = Boolean(message.connected)
      return
    }

    if (message.event === "error") {
      scanning = false
      scanTimeout.stop()
      online = Boolean(message.connected)
      processError = String(message.message || "")
      statusText = online ? String(message.action || "").toUpperCase() + " FAILED" : "OFFLINE"
      if (message.action === "pair-finish" || message.action === "add") {
        if (viewMode === "pin") {
          Qt.callLater(function() { pinInput.forceActiveFocus() })
        }
      }
      return
    }

    if (message.event !== "result") return

    online = true
    applyPlayback(message)
    if (message.action === "status") {
      statusText = powerStatusLabel(message.result || message.status)
    } else if (message.action === "text") {
      statusText = "SENT"
    } else {
      statusText = actionLabel(String(message.action || ""))
    }
  }

  function handleTextKey(text) {
    var key = String(text || "").toLowerCase()
    if (viewMode === "devices") {
      if (key === "r") scanDevices()
      else if (key === "b") backToRemote()
      else if (key === "a") hostInput.forceActiveFocus()
      else if (key === "x") removeSelectedDevice()
      else if (key === "q") close()
      return
    }
    if (viewMode !== "remote") return

    if (key === "b") sendAction("back")
    else if (key === "d") openDevices()
    else if (key === "g") sendAction("home")
    else if (key === "m") sendAction("menu")
    else if (key === "p") sendAction("play-pause")
    else if (key === "w") sendAction("wake")
    else if (key === "s" || key === "o") sendAction("power-off")
    else if (key === "x") sendAction("mute")
    else if (key === "+" || key === "=") sendAction("volume-up")
    else if (key === "-" || key === "_") sendAction("volume-down")
    else if (key === "[") sendAction("previous")
    else if (key === "]") sendAction("next")
    else if (key === "1") sendAction("app-plex")
    else if (key === "2") sendAction("app-netflix")
    else if (key === "3") sendAction("app-youtube")
    else if (key === "4") sendAction("app-disney")
    else if (key === "5") sendAction("app-prime")
    else if (key === "6") sendAction("app-settings")
    else if (key === "r") sendAction("rewind")
    else if (key === "f") sendAction("ff")
    else if (key === "/" || key === "t") searchInput.forceActiveFocus()
    else if (key === "q") close()
  }

  onPopupOpenChanged: {
    if (!popupOpen) {
      hoverHint = ""
      return
    }
    sendAction("status")
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  property int restartAttempts: 0
  readonly property int maxRestartAttempts: 5

  function manualReconnect() {
    restartAttempts = 0
    processError = ""
    sessionProcess.running = false
    sessionProcess.running = true
  }

  Timer {
    id: scanTimeout
    interval: 15000
    repeat: false
    onTriggered: {
      if (!root.scanning) return
      root.scanning = false
      if (root.devices.length === 0 && root.processError === "") {
        root.processError = "Scan timed out. Add the TV by IP if it does not appear."
        root.statusText = "SCAN TIMEOUT"
      }
    }
  }

  Process {
    id: sessionProcess
    command: [
      root.remotePath,
      "--host",
      root.host,
      "--name",
      root.deviceName,
      "session",
    ]
    environment: ({
      "PATH": "/usr/bin:/bin",
      "HOME": Quickshell.env("HOME") || "",
      "XDG_STATE_HOME": Quickshell.env("XDG_STATE_HOME") || "",
      "XDG_RUNTIME_DIR": Quickshell.env("XDG_RUNTIME_DIR") || "",
      "DBUS_SESSION_BUS_ADDRESS": Quickshell.env("DBUS_SESSION_BUS_ADDRESS") || "",
      "LC_ALL": "C.UTF-8"
    })
    stdinEnabled: true
    running: true

    stdout: SplitParser {
      onRead: function(line) {
        if (line && line.length <= 65536) {
          root.restartAttempts = 0
          root.handleSessionLine(line)
        }
      }
    }

    stderr: SplitParser {
      onRead: function(line) {
        if (line) root.processError = String(line).slice(0, 512).trim()
      }
    }

    onExited: function() {
      root.sessionReady = false
      root.online = false
      root.scanning = false
      scanTimeout.stop()
      root.statusText = "OFFLINE"
      if (root.restartAttempts < root.maxRestartAttempts) {
        root.restartAttempts++
        var delay = Math.min(10000, 1000 * Math.pow(2, root.restartAttempts - 1))
        sessionRestart.interval = delay
        sessionRestart.restart()
      } else {
        root.processError = "Backend stopped after multiple failures. Run setup or reconnect."
      }
    }
  }

  Timer {
    id: sessionRestart
    interval: 1000
    repeat: false
    onTriggered: {
      if (!sessionProcess.running) sessionProcess.running = true
    }
  }

  component RemoteKey: Button {
    property string action: ""
    property real keyWidth: 52
    property real keyHeight: 46

    width: keyWidth
    height: keyHeight
    tooltipText: root.actionTooltip(action)
    foreground: root.foreground
    accent: root.accent
    fontFamily: root.fontFamily
    fontSize: Style.font.bodySmall
    iconSize: Style.font.iconLarge
    bordered: true
    onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
    onClicked: root.sendAction(action)
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰟴"
    active: root.popupOpen
    tooltipText: root.activeDeviceName + (root.nowPlaying ? " · " + root.nowPlaying : " Android TV")
    onPressed: root.popupOpen = !root.popupOpen
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.popupOpen
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(332))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: pinInput.activeFocus || hostInput.activeFocus || nameInput.activeFocus || searchInput.activeFocus

      onMoveRequested: function(dx, dy) {
        if (root.viewMode === "devices") {
          root.moveDeviceCursor(dy)
        } else if (root.viewMode === "remote") {
          if (dx < 0) root.sendAction("left")
          else if (dx > 0) root.sendAction("right")
          else if (dy < 0) root.sendAction("up")
          else if (dy > 0) root.sendAction("down")
        }
      }
      onActivateRequested: {
        if (root.viewMode === "devices") root.activateSelectedDevice()
        else if (root.viewMode === "remote") root.sendAction("select")
      }
      onCloseRequested: {
        if (root.viewMode === "remote") root.close()
        else if (root.viewMode === "pin") root.cancelPairing()
        else root.backToRemote()
      }
      onTextKey: function(text) { root.handleTextKey(text) }

      Column {
        id: contentColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        Item {
          width: parent.width
          height: Math.max(titleBlock.implicitHeight, connectionLabel.implicitHeight)

          Row {
            id: titleBlock
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(9)

            Text {
              text: "󰍹"
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
            }

            Column {
              spacing: Style.space(1)

              Text {
                text: root.activeDeviceName.toUpperCase()
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
                font.bold: true
              }

              Text {
                text: root.viewMode === "remote" ? "ANDROID TV REMOTE"
                  : root.viewMode === "devices" ? "ANDROID TV DEVICES"
                  : "PAIR ANDROID TV"
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)

            Text {
              id: connectionLabel
              anchors.verticalCenter: parent.verticalCenter
              text: (root.online ? "● " : "○ ") + root.statusText
              textFormat: Text.PlainText
              color: root.online ? root.foreground : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            Button {
              width: 28
              height: 28
              text: ""
              iconText: "󰐥"
              tooltipText: "Sleep / power off  [S]"
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              visible: root.viewMode === "remote"
              onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
              onClicked: root.sendAction("power-off")
            }
          }
        }

        PanelSeparator {
          width: parent.width
          foreground: root.foreground
        }

        Text {
          width: parent.width
          text: root.hoverHint !== "" ? root.hoverHint : "Hover a key for its name"
          textFormat: Text.PlainText
          color: root.hoverHint !== "" ? root.foreground : root.dim
          wrapMode: Text.Wrap
          horizontalAlignment: Text.AlignHCenter
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: root.hoverHint !== ""
        }

        Column {
          id: remoteView
          visible: root.viewMode === "remote"
          width: parent.width
          spacing: Style.space(12)

          Item {
            width: parent.width
            height: playingLabel.implicitHeight

            Text {
              id: playingLabel
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - volumeCaption.implicitWidth - Style.space(8)
              text: root.playingLine
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: root.online ? root.foreground : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            Text {
              id: volumeCaption
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.volumeLine
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            RemoteKey {
              action: "back"
              iconText: "󰁍"
              text: "BACK"
              keyWidth: 92
            }

            RemoteKey {
              action: "home"
              iconText: "󰋜"
              text: "HOME"
              keyWidth: 92
            }

            RemoteKey {
              action: "menu"
              iconText: "󰍜"
              text: "MENU"
              keyWidth: 92
            }
          }

          Grid {
            anchors.horizontalCenter: parent.horizontalCenter
            columns: 3
            spacing: Style.space(7)

            Item { width: 56; height: 48 }
            RemoteKey { action: "up"; iconText: "󰜷"; keyWidth: 56; keyHeight: 48 }
            Item { width: 56; height: 48 }

            RemoteKey { action: "left"; iconText: "󰜱"; keyWidth: 56; keyHeight: 48 }
            RemoteKey { action: "select"; text: "OK"; keyWidth: 56; keyHeight: 48 }
            RemoteKey { action: "right"; iconText: "󰜴"; keyWidth: 56; keyHeight: 48 }

            Item { width: 56; height: 48 }
            RemoteKey { action: "down"; iconText: "󰜮"; keyWidth: 56; keyHeight: 48 }
            Item { width: 56; height: 48 }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            RemoteKey {
              action: "volume-down"
              iconText: "󰕿"
              text: "VOL−"
              keyWidth: 92
            }

            RemoteKey {
              action: "play-pause"
              iconText: "󰐎"
              keyWidth: 92
            }

            RemoteKey {
              action: "volume-up"
              iconText: "󰖀"
              text: "VOL+"
              keyWidth: 92
            }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            RemoteKey {
              action: "previous"
              iconText: "󰒮"
              text: "PREV"
              keyWidth: 52
            }

            RemoteKey {
              action: "rewind"
              iconText: "󰑈"
              text: "REW"
              keyWidth: 52
            }

            RemoteKey {
              action: "mute"
              iconText: "󰖁"
              text: "MUTE"
              keyWidth: 52
            }

            RemoteKey {
              action: "ff"
              iconText: "󰑐"
              text: "FF"
              keyWidth: 52
            }

            RemoteKey {
              action: "next"
              iconText: "󰒭"
              text: "NEXT"
              keyWidth: 52
            }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            RemoteKey {
              action: "wake"
              iconText: "󰤄"
              text: "WAKE"
              keyWidth: 142
            }

            RemoteKey {
              action: "power-off"
              iconText: "󰐥"
              text: "POWER OFF"
              keyWidth: 142
            }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            RemoteKey {
              action: "app-plex"
              text: "PLEX"
              keyWidth: 92
            }

            RemoteKey {
              action: "app-netflix"
              text: "NFLX"
              keyWidth: 92
            }

            RemoteKey {
              action: "app-youtube"
              text: "YT"
              keyWidth: 92
            }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            RemoteKey {
              action: "app-disney"
              text: "DSNY"
              keyWidth: 92
            }

            RemoteKey {
              action: "app-prime"
              text: "PRIME"
              keyWidth: 92
            }

            RemoteKey {
              action: "app-settings"
              text: "SET"
              keyWidth: 92
            }
          }

          TextField {
            id: searchInput
            width: parent.width
            placeholderText: "Type to search on the TV"
            foreground: root.foreground
            accent: root.accent
            onAccepted: root.sendSearch()
            Keys.onEscapePressed: function(event) {
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
          }

          Button {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 291
            height: 38
            text: "DEVICES"
            iconText: "󰒋"
            tooltipText: "Scan and switch TVs  [D]"
            onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            onClicked: root.openDevices()
          }

          Text {
            visible: root.processError !== ""
            width: parent.width
            text: root.processError
            textFormat: Text.PlainText
            color: root.accent
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Column {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(3)

            Repeater {
              model: [
                "[HJKL] MOVE   [ENTER] OK",
                "[B] BACK   [G] HOME   [M] MENU",
                "[P] PLAY   [R/F] REW/FF   [[/]] PREV/NEXT",
                "[−/+] VOL   [X] MUTE   [T] TYPE",
                "[1] PLEX  [2] NFLX  [3] YT  [4] DSNY  [5] PRIME  [6] SET",
                "[D] DEVICES   [ESC] CLOSE",
              ]

              Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: modelData
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        Column {
          id: devicesView
          visible: root.viewMode === "devices"
          width: parent.width
          spacing: Style.space(8)

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            Button {
              width: 142
              height: 38
              text: "BACK"
              iconText: "󰁍"
              tooltipText: "Back to remote  [B]"
              onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              onClicked: root.backToRemote()
            }

            Button {
              width: 142
              height: 38
              text: root.scanning ? "SCANNING" : "SCAN"
              iconText: "󰑓"
              iconSpinning: root.scanning
              tooltipText: "Rescan the network  [R]"
              onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              onClicked: root.scanDevices()
            }
          }

          TextField {
            id: hostInput
            width: parent.width
            placeholderText: "192.168.1.50"
            foreground: root.foreground
            accent: root.accent
            onAccepted: root.addHost()
            Keys.onEscapePressed: function(event) {
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
          }

          TextField {
            id: nameInput
            width: parent.width
            placeholderText: "Optional name"
            foreground: root.foreground
            accent: root.accent
            onAccepted: root.addHost()
            Keys.onEscapePressed: function(event) {
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
          }

          Button {
            width: parent.width
            height: 38
            text: "CONNECT HOST"
            iconText: "󰌘"
            tooltipText: "Add a TV by IP or hostname"
            onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
            foreground: root.foreground
            accent: root.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            onClicked: root.addHost()
          }

          Text {
            visible: root.processError !== ""
            width: parent.width
            text: root.processError
            textFormat: Text.PlainText
            color: root.accent
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            visible: !root.scanning && root.devices.length === 0 && root.processError === ""
            width: parent.width
            text: "NO DEVICES FOUND"
            textFormat: Text.PlainText
            color: root.dim
            horizontalAlignment: Text.AlignHCenter
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: root.devices

            Row {
              required property int index
              required property var modelData

              width: devicesView.width
              spacing: Style.space(7)

              Button {
                width: parent.width - 38 - parent.spacing
                height: 38
                text: String(modelData.name).toUpperCase()
                  + (modelData.paired ? "  ·  PAIRED" : "  ·  PAIR")
                  + (modelData.online ? "" : "  ·  OFFLINE")
                iconText: modelData.paired ? "󰌆" : "󰐕"
                tooltipText: (modelData.paired ? "Switch to " : "Pair with ")
                  + String(modelData.name || "device")
                  + (modelData.address ? "  ·  " + String(modelData.address) : "")
                selected: String(modelData.identifier) === root.activeIdentifier
                hasCursor: index === root.selectedDeviceIndex
                leftAlign: true
                foreground: modelData.online ? root.foreground : root.dim
                accent: root.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                bordered: true
                onHovered: function(isHovered) {
                  if (isHovered) root.selectedDeviceIndex = index
                  root.setHoverHint(tooltipText, isHovered)
                }
                onClicked: {
                  root.selectedDeviceIndex = index
                  root.activateSelectedDevice()
                }
              }

              Button {
                width: 38
                height: 38
                text: ""
                iconText: "󰆴"
                tooltipText: "Remove " + String(modelData.name || "device")
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                bordered: true
                onHovered: function(isHovered) {
                  if (isHovered) root.selectedDeviceIndex = index
                  root.setHoverHint(tooltipText, isHovered)
                }
                onClicked: {
                  root.selectedDeviceIndex = index
                  root.removeDevice(modelData.identifier)
                }
              }
            }
          }

          Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "[J/K] SELECT   [ENTER] OPEN   [X] REMOVE   [R] RESCAN"
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Column {
          id: pairingView
          visible: root.viewMode === "pin"
          width: parent.width
          spacing: Style.space(12)

          Text {
            width: parent.width
            text: "Enter the 6-character code shown on\n" + root.pairingName
            textFormat: Text.PlainText
            color: root.foreground
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          TextField {
            id: pinInput
            anchors.horizontalCenter: parent.horizontalCenter
            width: 180
            placeholderText: "A1B2C3"
            maximumLength: 6
            horizontalAlignment: TextInput.AlignHCenter
            inputMethodHints: Qt.ImhPreferUppercase
            validator: RegularExpressionValidator { regularExpression: /[0-9A-Fa-f]{0,6}/ }
            foreground: root.foreground
            accent: root.accent
            onAccepted: root.finishPairing(text)
            Keys.onEscapePressed: function(event) {
              root.cancelPairing()
              event.accepted = true
            }
          }

          Text {
            visible: root.processError !== ""
            width: parent.width
            text: root.processError
            textFormat: Text.PlainText
            color: root.accent
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(7)

            Button {
              width: 142
              height: 38
              text: "CANCEL"
              tooltipText: "Cancel pairing  [Esc]"
              onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              onClicked: root.cancelPairing()
            }

            Button {
              width: 142
              height: 38
              text: "PAIR"
              iconText: "󰌆"
              tooltipText: "Send the PIN shown on the TV  [Enter]"
              onHovered: function(isHovered) { root.setHoverHint(tooltipText, isHovered) }
              enabled: pinInput.text.length === 6
              opacity: enabled ? 1 : 0.5
              foreground: root.foreground
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              bordered: true
              onClicked: root.finishPairing(pinInput.text)
            }
          }

          Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "[ENTER] PAIR   [ESC] CANCEL"
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
