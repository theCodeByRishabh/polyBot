#!/bin/bash
# Starts bot + dashboard together (Railway compatible)
set -e

echo "Starting dashboard on port ${DASH_PORT:-8080}..."
python dashboard.py &
DASH_PID=$!

echo "Starting trading bot..."
python bot.py &
BOT_PID=$!

echo ""
echo "Both running. Dashboard → http://localhost:${DASH_PORT:-8080}"

cleanup() {
  echo "Stopping..."
  kill $BOT_PID $DASH_PID 2>/dev/null
  wait
  echo "Stopped."
}
trap cleanup SIGINT SIGTERM
wait $BOT_PID $DASH_PID