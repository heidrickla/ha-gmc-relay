#!/command/with-contenv sh
# shellcheck shell=sh
# with-contenv hands over the container's environment, SUPERVISOR_TOKEN included, which S6 overlay
# keeps from CMD; the relay needs it to find the MQTT broker. Options are read from /data/options.json.
set -eu

echo "[gmc-relay] serving on :80"
exec python3 /opt/gmc_relay/server.py --options /data/options.json --port 80 --ingress-port 8099 \
    --history /data/history.db --config-dir /config --counters /data/counters.json
