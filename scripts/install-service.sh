#!/usr/bin/env bash
# Runs autonome-context as a launchd agent (macOS): starts at login, restarts if it dies.
# Logs: ~/.autonome/logs/context.log. Remove with: launchctl bootout gui/$(id -u)/dev.autonome.context
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
PLIST="$HOME/Library/LaunchAgents/dev.autonome.context.plist"
mkdir -p "$HOME/.autonome/logs" "$(dirname "$PLIST")"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.autonome.context</string>
  <key>ProgramArguments</key>
  <array><string>$UV</string><string>run</string><string>--project</string><string>$ROOT/context</string><string>autonome-context</string></array>
  <key>WorkingDirectory</key><string>$ROOT/context</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.autonome/logs/context.log</string>
  <key>StandardErrorPath</key><string>$HOME/.autonome/logs/context.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/dev.autonome.context" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "autonome-context installed; health: curl http://127.0.0.1:8765/health"
