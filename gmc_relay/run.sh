#!/bin/sh
# Options are read by the relay from /data/options.json; nothing rides argv.
set -eu

echo "[gmc-relay] serving on :80"
exec python3 /opt/gmc_relay/server.py --options /data/options.json --port 80 --ingress-port 8099
