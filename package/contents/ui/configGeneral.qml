import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    id: page

    property string cfg_terminal
    property alias cfg_customCommand: customCommand.text
    property alias cfg_keepShellOpen: keepShell.checked
    property alias cfg_showUsage: showUsage.checked
    property alias cfg_showUsageInPanel: showUsageInPanel.checked
    property alias cfg_usageRefreshMinutes: usageRefresh.value
    property alias cfg_refreshSeconds: refreshSeconds.value
    property alias cfg_maxSessions: maxSessions.value

    readonly property var terminals: [
        { value: "auto", text: i18n("Automatic (the one Claude is already running in)") },
        { value: "alacritty", text: "Alacritty" },
        { value: "konsole", text: "Konsole" },
        { value: "kitty", text: "kitty" },
        { value: "ghostty", text: "Ghostty" },
        { value: "wezterm", text: "WezTerm" },
        { value: "foot", text: "foot" },
        { value: "gnome-terminal", text: "GNOME Terminal" },
        { value: "xterm", text: "xterm" },
        { value: "custom", text: i18n("Custom command…") }
    ]

    Kirigami.FormLayout {
        QQC2.ComboBox {
            Kirigami.FormData.label: i18n("Terminal:")
            model: page.terminals
            textRole: "text"
            valueRole: "value"
            Component.onCompleted: currentIndex = Math.max(0, indexOfValue(page.cfg_terminal))
            onActivated: page.cfg_terminal = currentValue
        }

        QQC2.TextField {
            id: customCommand
            Kirigami.FormData.label: i18n("Custom command:")
            visible: page.cfg_terminal === "custom"
            placeholderText: "foot -D {cwd} {cmd}"
            Layout.fillWidth: true
        }

        QQC2.Label {
            visible: customCommand.visible
            text: i18n("{cwd} is the project folder, {cmd} is the claude command.")
            font: Kirigami.Theme.smallFont
            opacity: 0.7
        }

        QQC2.CheckBox {
            id: keepShell
            text: i18n("Keep a shell open after Claude exits")
        }

        Item {
            Kirigami.FormData.isSection: true
        }

        QQC2.CheckBox {
            id: showUsage
            Kirigami.FormData.label: i18n("Plan usage:")
            text: i18n("Show usage limits in the popup")
        }

        QQC2.CheckBox {
            id: showUsageInPanel
            text: i18n("Show session usage % next to the icon")
        }

        QQC2.SpinBox {
            id: usageRefresh
            Kirigami.FormData.label: i18n("Check usage every:")
            from: 1
            to: 60
            textFromValue: (value) => i18np("%1 minute", "%1 minutes", value)
            valueFromText: (text) => parseInt(text)
        }

        Item {
            Kirigami.FormData.isSection: true
        }

        QQC2.SpinBox {
            id: refreshSeconds
            Kirigami.FormData.label: i18n("Rescan sessions every:")
            from: 5
            to: 600
            textFromValue: (value) => i18np("%1 second", "%1 seconds", value)
            valueFromText: (text) => parseInt(text)
        }

        QQC2.SpinBox {
            id: maxSessions
            Kirigami.FormData.label: i18n("Sessions listed:")
            from: 10
            to: 2000
            stepSize: 10
        }
    }
}
