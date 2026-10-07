#!/usr/bin/env bash
# Start the bot in a background tmux session named "bot".
#   ./scripts/start.sh        start (or report it is already running)
#   tmux attach -t bot        view logs   (detach: Ctrl+B then D)
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v tmux >/dev/null; then
  echo "tmux is not installed: sudo apt install -y tmux" >&2
  exit 1
fi
if tmux has-session -t bot 2>/dev/null; then
  echo "Bot is already running. View it with: tmux attach -t bot"
  exit 0
fi
if [ ! -x .venv/bin/python ]; then
  echo "No .venv found - run the installation steps in README.md first." >&2
  exit 1
fi

.venv/bin/python -m app.main --check-config
tmux new-session -d -s bot "bash -c '.venv/bin/python -m app.main; echo; echo Bot exited - press Enter to close.; read'"
echo "✅ Bot started. In Telegram: /menu then tap 🟢 Start."
echo "   Logs: tmux attach -t bot   (detach: Ctrl+B then D)"
