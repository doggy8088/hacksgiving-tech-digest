#!/bin/sh
# Entry point run as a Sprite service: serve the digest site and regenerate
# the digest every 6 hours.
cd "$(dirname "$0")"
python3 agent.py || true
python3 serve.py &
SERVER_PID=$!
while true; do
  sleep 21600
  python3 agent.py || true
done
