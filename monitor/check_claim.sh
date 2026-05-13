#!/usr/bin/env bash
# Polls claim.solstice.finance and fires a macOS notification the moment it
# flips from its current 503 "no healthy upstream" state to a live status.
# Runs via launchd every 15 minutes (see claim_monitor.plist).

set -u

URL="https://claim.solstice.finance/"
STATE_DIR="$HOME/Library/Application Support/solstice-claim-monitor"
LAST_FILE="$STATE_DIR/last_status"
LOG_FILE="$STATE_DIR/monitor.log"

mkdir -p "$STATE_DIR"

now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Probe with a short timeout. %{http_code} prints even on connect errors (000).
code=$(curl -sS -A "Mozilla/5.0" --max-time 15 -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null || echo "000")

prev="none"
[ -f "$LAST_FILE" ] && prev=$(cat "$LAST_FILE")

echo "$now  status=$code  prev=$prev" >> "$LOG_FILE"

# Only notify when the status changes to something NOT 503 (the "live" signal)
if [ "$code" != "$prev" ] && [ "$code" != "503" ]; then
  title="Solstice claim is LIVE 🚀"
  if [ "$code" = "200" ] || [ "$code" = "301" ] || [ "$code" = "302" ]; then
    msg="claim.solstice.finance returned $code (was $prev). TGE may have started."
  else
    msg="claim.solstice.finance returned $code (was $prev). Check manually."
  fi

  /usr/bin/osascript -e "display notification \"$msg\" with title \"$title\" sound name \"Glass\"" 2>/dev/null || true

  # Also say it aloud — hard to miss
  /usr/bin/say "Solstice claim URL is live" 2>/dev/null &

  echo "$now  ALERT  code=$code" >> "$LOG_FILE"
fi

# Always update last-known status, so we only alert on *changes*.
echo "$code" > "$LAST_FILE"
