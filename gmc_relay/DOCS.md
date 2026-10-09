# GMC Relay

Receive a GQ GMC Geiger counter's WiFi upload on port 80, hand it to Home Assistant, forward it to gmcmap.com.

## How the counter uploads

A GQ GMC-320+/500/500+/600/600+ with WiFi fetches, every `Period` minutes,

```
GET http://<Website>/<URL>?AID=<user id>&GID=<counter id>&CPM=<n>&ACPM=<n>&uSV=<n>
```

on port 80. The defaults are Website `www.gmcmap.com`, URL `log2.asp`. Set Website to this host's LAN address (Menu, WiFi, Website) and leave the rest.

## What the relay does

| Step | Detail |
|---|---|
| Accept | `GET /log2.asp` or `/log2.php` with a numeric `CPM`; `ACPM` and `uSV` optional. Query keys in any letter case. An upload without a numeric `CPM` gets 400. |
| Check | If `counter_ids` is set, a `GID` not in it is refused with 403 and not forwarded. |
| Home Assistant | POST `{"cpm", "acpm", "usv", "gid", "aid", "path", "received"}` to the webhook `webhook_id` at `core_url`, straight to core on the apps network. |
| gmcmap | With `forward` on, the exact request is replayed to `forward_url` and the counter gets gmcmap's reply. |
| Reply | `OK.ERR0.`, gmcmap's success string, when not forwarding or when gmcmap fails; the reading is already in Home Assistant. |

`GET /healthz` answers `ok` for the watchdog; `GET /status.json` shows the count of readings passed on and the last one.

## Status panel

The sidebar entry GMC Relay shows the readings passed on, the last reading with its time, the forward target and whether the webhook is set. It reloads every 30 seconds. It is served on port 8099 to Home Assistant's ingress gateway only.

## Options

| Option | Default | Effect |
|---|---|---|
| `webhook_id` | | The Home Assistant webhook that receives readings. Any string; keep it private. |
| `counter_ids` | `[]` | Accepted `GID` values. Empty accepts any counter. |
| `forward` | true | Replay each upload to `forward_url`. |
| `forward_url` | `https://www.gmcmap.com` | Where to replay. |
| `core_url` | `https://homeassistant:8123` | Where the webhook is posted. The default is core as every app reaches it; use `http://` if Home Assistant has no certificate. |
| `log_level` | info | `debug` logs every request. |

## Home Assistant side

A trigger-based template sensor turns the webhook into entities:

```yaml
template:
  - triggers:
      - trigger: webhook
        webhook_id: <the same webhook_id>
        allowed_methods: [POST]
        local_only: true
    sensor:
      - name: geiger_meter_cpm
        unique_id: geiger_meter_cpm
        unit_of_measurement: CPM
        state_class: measurement
        state: "{{ trigger.json.cpm }}"
      - name: geiger_meter_usv
        unique_id: geiger_meter_usv
        unit_of_measurement: "µSv/h"
        state_class: measurement
        state: "{{ trigger.json.usv }}"
```

`local_only: true` keeps the webhook off the internet; the apps network is local. The Supervisor's core proxy is not used: it answers 200 to a webhook POST without delivering it. The reading arrives as fast as the counter's upload period, with no polling and no dependence on gmcmap's HTML.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Log says `webhook NOT SET` | `webhook_id` empty | Set it and restart. |
| `webhook refused the reading` | `core_url` wrong, or Home Assistant restarting | Core answers 200 even for an unknown id, so a refusal is a transport problem, not the id. |
| Relay says 200 but no state | Webhook id differs from the template trigger, or the trigger is not loaded | Same id on both sides; check the template block loaded. |
| Nothing arrives | The counter still points at gmcmap, or has no route to this host | Check the counter's Website setting and the LAN. `status.json` counts what arrived. |
| `forward ... failed` | gmcmap unreachable or the counter id unknown there | The reading is in Home Assistant regardless. |
