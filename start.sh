#!/bin/bash
# Starts bot + dashboard together
# Usage: bash start.sh
set -e

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Run: cp .env.example .env  then fill in your keys."
  exit 1
fi

export $(grep -v '^#' .env | xargs)

echo "Starting dashboard on port ${DASH_PORT:-8080}..."
python dashboard.py &
DASH_PID=$!

echo "Starting trading bot..."
python bot.py &
BOT_PID=$!

echo ""
echo "Both running. Dashboard → http://localhost:${DASH_PORT:-8080}"
echo "Press Ctrl+C to stop both."

cleanup() {
  echo "Stopping..."
  kill $BOT_PID $DASH_PID 2>/dev/null
  wait
  echo "Stopped."
}
trap cleanup SIGINT SIGTERM
wait $BOT_PID $DASH_PID
