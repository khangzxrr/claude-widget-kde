#!/bin/sh
# Install or upgrade the Claude Sessions plasmoid for the current user.
set -e
cd "$(dirname "$0")"

ID=com.github.khangzxrr.claudesessions

if kpackagetool6 -t Plasma/Applet -s "$ID" >/dev/null 2>&1; then
    kpackagetool6 -t Plasma/Applet -u package
    echo "Upgraded. If the widget is already on a panel, restart Plasma to load the new code:"
    echo "  systemctl --user restart plasma-plasmashell"
else
    kpackagetool6 -t Plasma/Applet -i package
    echo "Installed. Right-click your panel → Add or Manage Widgets → search \"Claude Sessions\"."
fi
