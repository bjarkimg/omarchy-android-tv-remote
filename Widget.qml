import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.bjarkimg.shield-remote"

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

  readonly property string deviceName: String(setting("deviceName", "SHIELD"))
  readonly property string host: String(setting("host", ""))
  readonly property string remotePath: decodeURIComponent(
    String(Qt.resolvedUrl("shield-remote")).replace(/^file:\/\//, "")
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
  }

  function open() {
    popupOpen = true
  }

  function actionLabel(action) {
    var value = String(action || "")
    if (value.indexOf("app-") === 0) return value.slice(4).toUpperCase()
    if (value === "ff") return "FAST FORWARD"
    return value.toUpperCase().replace(/-/g, " ")
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

  function openDevices() {
    viewMode = "devices"
    processError = ""
    scanDevices()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function scanDevices() {
    processError = ""
    if (sendRequest({ "op": "discover" })) {
      scanning = true
      statusText = "SCANNING"
    } else {
      scanning = false
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
    pairingName = String(name || "SHIELD")
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
      online = true
      updateActiveDevice(message)
      statusText = powerStatusLabel(message.status)
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

    if (message.event === "devices") {
      scanning = false
      devices = message.devices || []
      selectedDeviceIndex = 0
      for (var index = 0; index < devices.length; index++) {
        if (String(devices[index].identifier) === activeIdentifier) {
          selectedDeviceIndex = index
          break
        }
      }
      statusText = String(devices.length) + " FOUND"
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
      sessionReady = true
      online = true
      viewMode = "remote"
      statusText = message.event === "paired" ? "PAIRED" : "ONLINE"
      processError = ""
      flushQueuedActions()
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
      return
    }

    if (message.event === "error") {
      scanning = false
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
    if (message.action === "status") {
      statusText = powerStatusLabel(message.result)
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
    else if (key === "s") sendAction("sleep")
    else if (key === "+" || key === "=") sendAction("volume-up")
    else if (key === "-" || key === "_") sendAction("volume-down")
    else if (key === "1") sendAction("app-plex")
    else if (key === "2") sendAction("app-netflix")
    else if (key === "3") sendAction("app-youtube")
    else if (key === "r") sendAction("rewind")
    else if (key === "f") sendAction("ff")
    else if (key === "q") close()
  }

  onPopupOpenChanged: {
    if (!popupOpen) return
    sendAction("status")
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

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
    stdinEnabled: true
    running: true

    stdout: SplitParser {
      onRead: function(line) { root.handleSessionLine(line) }
    }

    stderr: SplitParser {
      onRead: function(line) { root.processError = String(line || "").trim() }
    }

    onExited: function() {
      root.sessionReady = false
      root.online = false
      root.scanning = false
      root.statusText = root.reconnecting ? "RECONNECTING" : "OFFLINE"
      sessionRestart.restart()
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
    foreground: root.foreground
    accent: root.accent
    fontFamily: root.fontFamily
    fontSize: Style.font.bodySmall
    iconSize: Style.font.iconLarge
    bordered: true
    onClicked: root.sendAction(action)
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰟴"
    active: root.popupOpen
    tooltipText: root.activeDeviceName + " SHIELD"
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
      blocked: pinInput.activeFocus || hostInput.activeFocus || nameInput.activeFocus

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
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
            }

            Column {
              spacing: Style.space(1)

              Text {
                text: root.activeDeviceName.toUpperCase()
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
                font.bold: true
              }

              Text {
                text: root.viewMode === "remote" ? "SHIELD REMOTE"
                  : root.viewMode === "devices" ? "SHIELD DEVICES"
                  : "PAIR SHIELD"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Text {
            id: connectionLabel
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: (root.online ? "● " : "○ ") + root.statusText
            color: root.online ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }
        }

        PanelSeparator {
          width: parent.width
          foreground: root.foreground
        }

        Column {
          id: remoteView
          visible: root.viewMode === "remote"
          width: parent.width
          spacing: Style.space(12)

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
              action: "rewind"
              iconText: "󰑈"
              text: "REW"
              keyWidth: 92
            }

            RemoteKey {
              action: "ff"
              iconText: "󰑐"
              text: "FF"
              keyWidth: 92
            }

            RemoteKey {
              action: "wake"
              iconText: "󰤄"
              text: "WAKE"
              keyWidth: 92
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

          Button {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 291
            height: 38
            text: "DEVICES"
            iconText: "󰒋"
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
                "[P] PLAY   [R/F] REW/FF   [−/+] VOL",
                "[1] PLEX   [2] NETFLIX   [3] YOUTUBE",
                "[D] DEVICES   [ESC] CLOSE",
              ]

              Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: modelData
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
            color: root.accent
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            visible: !root.scanning && root.devices.length === 0 && root.processError === ""
            width: parent.width
            text: "NO SHIELDS FOUND"
            color: root.dim
            horizontalAlignment: Text.AlignHCenter
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: root.devices

            Button {
              required property int index
              required property var modelData

              width: devicesView.width
              height: 38
              text: String(modelData.name).toUpperCase()
                + (modelData.paired ? "  ·  PAIRED" : "  ·  PAIR")
                + (modelData.online ? "" : "  ·  OFFLINE")
              iconText: modelData.paired ? "󰌆" : "󰐕"
              tooltipText: String(modelData.address || modelData.host || "")
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
              }
              onClicked: {
                root.selectedDeviceIndex = index
                root.activateSelectedDevice()
              }
            }
          }

          Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "[J/K] SELECT   [ENTER] OPEN   [R] RESCAN   [ESC] BACK"
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
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
