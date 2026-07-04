#!/bin/bash
# Kill any running editor server and relaunch it FULLY DETACHED.
#
# Why the detachment matters: launching with a plain `nohup python3 server.py &`
# leaves the process in the shell's process group. When the terminal is closed or
# the job loses the foreground, macOS suspends it with a terminal-stop signal
# (SIGTTIN/SIGTTOU) -> the server freezes in state "T"/"TN", stops answering, and
# the editor UI hangs (e.g. an export stuck on "PREP"). nohup does NOT block those
# signals. Detaching into a new session via os.setsid() does, so the server keeps
# running no matter what happens to the terminal.
set -e
cd "$(dirname "$0")/.."

# 1) stop any existing server (and free the port if something else grabbed it)
pkill -9 -f "editor/server.py" 2>/dev/null || true
sleep 1
lsof -ti :8000 2>/dev/null | xargs -r kill -9 2>/dev/null || true

# 2) relaunch in its own session (os.setsid), stdin from /dev/null, logs to a file
nohup python3 -c "import os; os.setsid(); os.execvp('python3', ['python3', 'editor/server.py'])" \
  < /dev/null > /tmp/editor-server.log 2>&1 &
disown 2>/dev/null || true

# 3) wait briefly and report
sleep 2
pid=$(pgrep -f "editor/server.py" | head -1)
state=$(ps -p "$pid" -o state= 2>/dev/null | tr -d ' ')
code=$(curl -s -m 6 -o /dev/null -w "%{http_code}" http://localhost:8000/ 2>/dev/null || echo "000")
echo "editor server: pid=$pid state=$state  http=$code  (state should contain 's' = detached session, never 'T')"
echo "logs: /tmp/editor-server.log   ·   open: http://localhost:8000"
