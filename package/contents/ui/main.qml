import QtQuick
import QtQuick.Layouts
import QtQuick.Dialogs as Dialogs
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    readonly property string backendPath: decodeURIComponent(Qt.resolvedUrl("../code/backend.py").toString().replace(/^file:\/\//, ""))
    readonly property url iconUrl: Qt.resolvedUrl("../icons/claude-sessions.svg")

    property var sessions: []
    property var projects: []
    property var usage: ({ limits: [], error: null })
    property string sessionsError: ""
    property string lastSessionsJson: ""
    property string searchText: ""
    property double now: Date.now()

    readonly property var sessionLimit: (usage.limits || []).find(l => l.group === "session")
    readonly property int runningCount: sessions.filter(s => s.live).length
    readonly property int waitingCount: sessions.filter(s => s.live && s.live.status === "idle").length

    Plasmoid.icon: iconUrl
    switchWidth: Kirigami.Units.gridUnit * 16
    switchHeight: Kirigami.Units.gridUnit * 18

    toolTipMainText: i18n("Claude Sessions")
    toolTipSubText: {
        const lines = []
        if (usage.limits && usage.limits.length)
            lines.push(usage.limits.filter(l => !l.label.includes("·")).map(l => `${l.label} ${l.percent}%`).join(" · "))
        if (runningCount)
            lines.push(waitingCount ? i18n("%1 running, %2 waiting for you", runningCount, waitingCount) : i18np("%1 running", "%1 running", runningCount))
        lines.push(i18n("Middle-click to resume the latest session"))
        return lines.join("\n")
    }

    // ------------------------------------------------------------ backend

    P5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        property var callbacks: ({})

        onNewData: (sourceName, data) => {
            const callback = callbacks[sourceName]
            delete callbacks[sourceName]
            disconnectSource(sourceName)
            if (callback)
                callback(data["exit code"], data["stdout"], data["stderr"])
        }

        function run(argv, callback) {
            const command = argv.map(root.shellQuote).join(" ")
            if (command in callbacks)
                return
            callbacks[command] = callback
            connectSource(command)
        }
    }

    function shellQuote(value) {
        return "'" + String(value).replace(/'/g, "'\\''") + "'"
    }

    function backend(args, callback) {
        executable.run(["python3", backendPath].concat(args), callback || (() => {}))
    }

    function terminalArgs() {
        const cfg = Plasmoid.configuration
        return ["--terminal", cfg.terminal, "--command", cfg.customCommand].concat(cfg.keepShellOpen ? ["--keep-shell"] : [])
    }

    function refreshSessions(force) {
        backend(["list", "--limit", Plasmoid.configuration.maxSessions], (code, stdout, stderr) => {
            if (code !== 0) {
                sessionsError = (stderr || stdout || i18n("Backend failed")).trim().split("\n").pop()
                return
            }
            sessionsError = ""
            now = Date.now()
            if (!force && stdout === lastSessionsJson)
                return
            lastSessionsJson = stdout
            try {
                const data = JSON.parse(stdout)
                sessions = data.sessions
                projects = data.projects
                rebuildModel()
            } catch (e) {
                sessionsError = String(e)
            }
        })
    }

    function refreshUsage(force) {
        if (!Plasmoid.configuration.showUsage && !Plasmoid.configuration.showUsageInPanel)
            return
        const maxAge = force ? 0 : Plasmoid.configuration.usageRefreshMinutes * 60 - 5
        backend(["usage", "--max-age", maxAge], (code, stdout) => {
            try {
                usage = JSON.parse(stdout)
            } catch (e) {}
        })
    }

    function openSession(sessionId) {
        root.expanded = false
        backend(["open", sessionId].concat(terminalArgs()))
    }

    function newSession(folder) {
        root.expanded = false
        backend(["new", folder].concat(terminalArgs()))
    }

    function resumeLatest() {
        const latest = sessions.slice().sort((a, b) => b.modified - a.modified)[0]
        if (latest)
            openSession(latest.id)
    }

    function copyText(text) {
        clipboardHelper.text = text
        clipboardHelper.selectAll()
        clipboardHelper.copy()
    }

    TextEdit {
        id: clipboardHelper
        visible: false
    }

    // ------------------------------------------------------------ model

    ListModel {
        id: sessionModel
    }

    function groupOf(session) {
        if (session.live)
            return i18n("Running now")
        const today = new Date(now)
        today.setHours(0, 0, 0, 0)
        const dayMs = 86400000
        if (session.modified >= today.getTime())
            return i18n("Today")
        if (session.modified >= today.getTime() - dayMs)
            return i18n("Yesterday")
        if (session.modified >= today.getTime() - 6 * dayMs)
            return i18n("This week")
        return i18n("Older")
    }

    function rebuildModel() {
        const query = searchText.trim().toLowerCase()
        const rows = []
        for (const s of sessions) {
            if (query && ![s.title, s.projectDisplay, s.branch, s.firstPrompt, s.lastPrompt, s.id].join("\n").toLowerCase().includes(query))
                continue
            rows.push({
                sessionId: s.id,
                title: s.title,
                project: s.project,
                projectDisplay: s.projectDisplay,
                projectExists: s.projectExists,
                branch: s.branch,
                lastPrompt: s.lastPrompt,
                prompts: s.prompts,
                modified: s.modified,
                liveStatus: s.live ? s.live.status : "",
                group: groupOf(s)
            })
        }
        // update rows in place when the shape is unchanged so hover/scroll state survives refreshes
        const sameShape = rows.length === sessionModel.count
            && rows.every((r, i) => sessionModel.get(i).sessionId === r.sessionId && sessionModel.get(i).group === r.group)
        if (sameShape) {
            rows.forEach((r, i) => sessionModel.set(i, r))
        } else {
            sessionModel.clear()
            rows.forEach(r => sessionModel.append(r))
        }
    }

    onSearchTextChanged: rebuildModel()

    function timeAgo(ms) {
        const seconds = Math.max(0, (now - ms) / 1000)
        if (seconds < 60) return i18n("just now")
        if (seconds < 3600) return i18n("%1m ago", Math.floor(seconds / 60))
        if (seconds < 86400) return i18n("%1h ago", Math.floor(seconds / 3600))
        if (seconds < 7 * 86400) return i18n("%1d ago", Math.floor(seconds / 86400))
        return new Date(ms).toLocaleDateString(Qt.locale(), Locale.ShortFormat)
    }

    function timeUntil(ms) {
        const minutes = Math.max(0, Math.round((ms - now) / 60000))
        if (minutes < 60) return i18n("%1m", minutes)
        if (minutes < 24 * 60) return i18n("%1h %2m", Math.floor(minutes / 60), minutes % 60)
        return i18n("%1d %2h", Math.floor(minutes / 1440), Math.floor((minutes % 1440) / 60))
    }

    function usageColor(percent) {
        if (percent >= 90) return Kirigami.Theme.negativeTextColor
        if (percent >= 70) return Kirigami.Theme.neutralTextColor
        return Kirigami.Theme.highlightColor
    }

    // ------------------------------------------------------------ timers

    Timer {
        interval: (root.expanded ? 5 : Math.max(5, Plasmoid.configuration.refreshSeconds)) * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshSessions(false)
    }

    Timer {
        interval: Math.max(1, Plasmoid.configuration.usageRefreshMinutes) * 60000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshUsage(false)
    }

    Timer {
        interval: 30000
        running: true
        repeat: true
        onTriggered: root.now = Date.now()
    }

    onExpandedChanged: {
        if (expanded) {
            now = Date.now()
            refreshSessions(true)
            refreshUsage(false)
        } else {
            searchText = ""
        }
    }

    // ------------------------------------------------------------ panel

    compactRepresentation: MouseArea {
        id: compact

        readonly property bool horizontal: Plasmoid.formFactor === PlasmaCore.Types.Horizontal
        readonly property bool showPercent: horizontal && Plasmoid.configuration.showUsageInPanel && root.sessionLimit !== undefined
        property bool wasExpanded: false

        Layout.minimumWidth: horizontal ? compactRow.implicitWidth : Kirigami.Units.iconSizes.small
        Layout.maximumWidth: horizontal ? compactRow.implicitWidth : Infinity
        Layout.minimumHeight: horizontal ? Kirigami.Units.iconSizes.small : width

        hoverEnabled: true
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        onPressed: wasExpanded = root.expanded
        onClicked: (mouse) => {
            if (mouse.button === Qt.MiddleButton)
                root.resumeLatest()
            else
                root.expanded = !wasExpanded
        }

        RowLayout {
            id: compactRow
            anchors.fill: parent
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Icon {
                id: panelIcon
                Layout.preferredWidth: compact.horizontal ? compact.height : compact.width
                Layout.preferredHeight: Layout.preferredWidth
                source: root.iconUrl
                active: compact.containsMouse

                Rectangle {
                    visible: root.waitingCount > 0
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    width: Math.max(6, Math.round(parent.width * 0.32))
                    height: width
                    radius: width / 2
                    color: Kirigami.Theme.positiveTextColor
                    border.color: Kirigami.Theme.backgroundColor
                    border.width: 1
                }
            }

            PlasmaComponents3.Label {
                visible: compact.showPercent
                text: compact.showPercent ? root.sessionLimit.percent + "%" : ""
                color: compact.showPercent && root.sessionLimit.percent >= 70 ? root.usageColor(root.sessionLimit.percent) : Kirigami.Theme.textColor
                font.pixelSize: Math.round(compact.height * 0.42)
                Layout.rightMargin: Kirigami.Units.smallSpacing
            }
        }
    }

    // ------------------------------------------------------------ popup

    fullRepresentation: PlasmaExtras.Representation {
        id: popup

        Layout.minimumWidth: Kirigami.Units.gridUnit * 20
        Layout.preferredWidth: Kirigami.Units.gridUnit * 26
        Layout.minimumHeight: Kirigami.Units.gridUnit * 20
        Layout.preferredHeight: Kirigami.Units.gridUnit * 34
        collapseMarginsHint: true

        Connections {
            target: root
            function onExpandedChanged() {
                if (root.expanded) {
                    search.text = ""
                    sessionList.currentIndex = -1
                    search.forceActiveFocus()
                }
            }
        }

        header: PlasmaExtras.PlasmoidHeading {
            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    Layout.fillWidth: true

                    PlasmaExtras.SearchField {
                        id: search
                        Layout.fillWidth: true
                        placeholderText: i18n("Search sessions, folders, prompts…")
                        onTextChanged: root.searchText = text
                        Keys.onDownPressed: {
                            if (sessionList.count) {
                                sessionList.currentIndex = 0
                                sessionList.forceActiveFocus()
                            }
                        }
                        Keys.onReturnPressed: {
                            if (sessionList.count)
                                root.openSession(sessionModel.get(Math.max(0, sessionList.currentIndex)).sessionId)
                        }
                    }

                    PlasmaComponents3.ToolButton {
                        icon.name: "list-add"
                        onClicked: newMenu.popup(this, 0, height)
                        PlasmaComponents3.ToolTip { text: i18n("New session in…") }
                        Accessible.name: i18n("New session")

                        PlasmaComponents3.Menu {
                            id: newMenu

                            Instantiator {
                                model: root.projects
                                delegate: PlasmaComponents3.MenuItem {
                                    text: modelData.display
                                    icon.name: "folder"
                                    onTriggered: root.newSession(modelData.path)
                                }
                                onObjectAdded: (index, object) => newMenu.insertItem(index, object)
                                onObjectRemoved: (index, object) => newMenu.removeItem(object)
                            }

                            PlasmaComponents3.MenuSeparator {
                                visible: root.projects.length > 0
                            }

                            PlasmaComponents3.MenuItem {
                                text: i18n("Choose folder…")
                                icon.name: "folder-open"
                                onTriggered: folderDialog.open()
                            }
                        }
                    }

                    PlasmaComponents3.ToolButton {
                        icon.name: "view-refresh"
                        onClicked: {
                            root.refreshSessions(true)
                            root.refreshUsage(true)
                        }
                        PlasmaComponents3.ToolTip { text: i18n("Refresh") }
                        Accessible.name: i18n("Refresh")
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                    Layout.leftMargin: Kirigami.Units.smallSpacing
                    Layout.rightMargin: Kirigami.Units.smallSpacing
                    visible: Plasmoid.configuration.showUsage && ((root.usage.limits || []).length > 0 || !!root.usage.error)
                    spacing: Kirigami.Units.smallSpacing

                    Repeater {
                        model: root.usage.limits || []

                        delegate: ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                Layout.fillWidth: true
                                PlasmaComponents3.Label {
                                    text: modelData.label
                                    font: Kirigami.Theme.smallFont
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                }
                                PlasmaComponents3.Label {
                                    text: modelData.resetsAt
                                        ? i18n("%1% · resets in %2", modelData.percent, root.timeUntil(modelData.resetsAt))
                                        : i18n("%1%", modelData.percent)
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.75
                                }
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                implicitHeight: 4
                                radius: 2
                                color: Qt.alpha(Kirigami.Theme.textColor, 0.12)

                                Rectangle {
                                    width: parent.width * Math.min(100, modelData.percent) / 100
                                    height: parent.height
                                    radius: parent.radius
                                    color: root.usageColor(modelData.percent)
                                }
                            }
                        }
                    }

                    PlasmaComponents3.Label {
                        visible: !!root.usage.error
                        text: root.usage.error || ""
                        font: Kirigami.Theme.smallFont
                        color: Kirigami.Theme.neutralTextColor
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }
            }
        }

        PlasmaComponents3.ScrollView {
            anchors.fill: parent

            ListView {
                id: sessionList
                model: sessionModel
                clip: true
                currentIndex: -1
                keyNavigationEnabled: true
                highlightMoveDuration: 0
                reuseItems: true

                section.property: "group"
                section.delegate: PlasmaComponents3.Label {
                    required property string section
                    width: ListView.view.width
                    text: section
                    font.weight: Font.DemiBold
                    opacity: 0.7
                    elide: Text.ElideRight
                    leftPadding: Kirigami.Units.largeSpacing
                    rightPadding: Kirigami.Units.largeSpacing
                    topPadding: Kirigami.Units.largeSpacing
                    bottomPadding: Kirigami.Units.smallSpacing
                }

                Keys.onReturnPressed: {
                    if (currentIndex >= 0)
                        root.openSession(sessionModel.get(currentIndex).sessionId)
                }
                Keys.onUpPressed: (event) => {
                    if (currentIndex <= 0)
                        search.forceActiveFocus()
                    else
                        decrementCurrentIndex()
                }

                delegate: PlasmaComponents3.ItemDelegate {
                    id: row

                    required property int index
                    required property var model

                    width: ListView.view.width
                    highlighted: ListView.isCurrentItem
                    onClicked: root.openSession(model.sessionId)
                    Accessible.name: model.title

                    contentItem: RowLayout {
                        spacing: Kirigami.Units.largeSpacing

                        Rectangle {
                            Layout.alignment: Qt.AlignTop
                            Layout.topMargin: Math.round(titleLabel.implicitHeight / 2 - height / 2)
                            implicitWidth: Kirigami.Units.smallSpacing * 2
                            implicitHeight: implicitWidth
                            radius: width / 2
                            color: row.model.liveStatus === "busy" ? Kirigami.Theme.neutralTextColor
                                 : row.model.liveStatus ? Kirigami.Theme.positiveTextColor
                                 : "transparent"
                            border.width: row.model.liveStatus ? 0 : 1
                            border.color: Qt.alpha(Kirigami.Theme.textColor, 0.3)

                            SequentialAnimation on opacity {
                                running: row.model.liveStatus === "busy"
                                loops: Animation.Infinite
                                onRunningChanged: if (!running) parent.opacity = 1
                                NumberAnimation { to: 0.35; duration: 700; easing.type: Easing.InOutQuad }
                                NumberAnimation { to: 1; duration: 700; easing.type: Easing.InOutQuad }
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0

                            PlasmaComponents3.Label {
                                id: titleLabel
                                Layout.fillWidth: true
                                text: row.model.title
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.weight: Font.DemiBold
                            }

                            PlasmaComponents3.Label {
                                Layout.fillWidth: true
                                textFormat: Text.PlainText
                                elide: Text.ElideMiddle
                                font: Kirigami.Theme.smallFont
                                opacity: 0.75
                                text: [
                                    row.model.liveStatus === "busy" ? i18n("working")
                                        : row.model.liveStatus === "idle" ? i18n("waiting for you")
                                        : row.model.liveStatus ? row.model.liveStatus
                                        : root.timeAgo(row.model.modified),
                                    row.model.projectDisplay,
                                    row.model.branch,
                                    i18np("%1 prompt", "%1 prompts", row.model.prompts)
                                ].filter(Boolean).join("  ·  ")
                            }

                            PlasmaComponents3.Label {
                                Layout.fillWidth: true
                                visible: text.length > 0
                                text: row.model.lastPrompt && row.model.lastPrompt !== row.model.title ? "› " + row.model.lastPrompt : ""
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font: Kirigami.Theme.smallFont
                                opacity: 0.55
                            }
                        }

                        RowLayout {
                            spacing: 0
                            opacity: row.hovered || row.highlighted ? 1 : 0
                            Behavior on opacity { NumberAnimation { duration: Kirigami.Units.shortDuration } }

                            PlasmaComponents3.ToolButton {
                                icon.name: "edit-copy"
                                onClicked: root.copyText(`cd ${root.shellQuote(row.model.project)} && claude --resume ${row.model.sessionId}`)
                                PlasmaComponents3.ToolTip { text: i18n("Copy resume command") }
                                Accessible.name: i18n("Copy resume command")
                            }

                            PlasmaComponents3.ToolButton {
                                icon.name: "folder-open"
                                enabled: row.model.projectExists
                                onClicked: {
                                    root.expanded = false
                                    Qt.openUrlExternally("file://" + row.model.project)
                                }
                                PlasmaComponents3.ToolTip { text: i18n("Open project folder") }
                                Accessible.name: i18n("Open project folder")
                            }
                        }
                    }
                }

                PlasmaExtras.PlaceholderMessage {
                    anchors.centerIn: parent
                    width: parent.width - Kirigami.Units.gridUnit * 4
                    visible: sessionList.count === 0
                    iconName: root.sessionsError ? "dialog-error" : root.searchText ? "edit-find" : "utilities-terminal"
                    text: root.sessionsError ? i18n("Could not read sessions")
                        : root.searchText ? i18n("No matching sessions")
                        : i18n("No Claude Code sessions yet")
                    explanation: root.sessionsError
                }
            }
        }

        Dialogs.FolderDialog {
            id: folderDialog
            title: i18n("Start a Claude session in…")
            onAccepted: root.newSession(decodeURIComponent(selectedFolder.toString().replace(/^file:\/\//, "")))
        }
    }
}
