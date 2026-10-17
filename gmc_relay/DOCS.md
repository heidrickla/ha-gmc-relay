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
| Accept | `GET /log2.asp` or `/log2.php` with a numeric `CPM`; `ACPM` and `uSV` optional. Query keys in any letter case. An upload without a finite numeric `CPM` gets 400. |
| Keep | The reading goes into the history behind the panel. |
| Check | If `counter_ids` is set, a `GID` not in it is refused with 403 and not forwarded. |
| Home Assistant | POST `{"cpm", "acpm", "usv", "gid", "aid", "path", "received"}` to the webhook `webhook_id` at `core_url`, straight to core on the apps network. |
| gmcmap | With `forward` on, the exact request is replayed to `forward_url` and the counter gets gmcmap's reply. |
| Reply | `OK.ERR0.`, gmcmap's success string, when not forwarding or when gmcmap fails; the reading is already in Home Assistant. |

`GET /healthz` answers `ok` for the watchdog; `GET /status.json` shows the count of readings passed on and the last one.

## Panel

The sidebar entry GMC Relay, served on port 8099 to Home Assistant's ingress gateway only.

| Part | Shows |
|---|---|
| Header | The counter id, the last webhook answer from Home Assistant and the last answer from gmcmap. |
| Dose rate now | The latest µSv/h, CPM and ACPM, the time since the upload, and the last 60 readings' CPM. |
| Past year | Each day's average dose rate as a calendar; fewer weeks on a narrow screen. |
| Filters | Range 24 h, 7 d, 30 d, 90 d, 1 y or all; CPM or µSv/h; a counter picker when more than one counter has uploaded. |
| Tiles | Average with its change against the period before; highest reading and when; estimated dose (average µSv/h times the hours covered) with its yearly rate; reading count. |
| Trend | The average per bucket with the lowest-to-highest band; buckets of 5 min for 24 h up to 1 day for a year. A gap of more than two buckets breaks the line. Hover or arrow keys for values; Table lists every bucket. |
| Hour of day | Each local hour's average as a difference from the range's average. |
| Count distribution | How often each CPM value was read, with the Poisson distribution of the same mean. |

The status refreshes every 15 seconds and the charts every minute. Dark by default; the theme, range and measure are kept in the browser.

## History

Every reading that passes the counter check is kept in `/data/history.db` (SQLite), whether or not Home Assistant accepted the webhook; a minute's upload period adds about 25 MB a year. It is part of the app's backups.

To seed it, put `import.csv` in the app's config folder (`/addon_configs/<repository>_gmc_relay/` on the host, `local_gmc_relay` for a local copy) and restart. It is read once and renamed `import.csv.done`; rows already in the history are skipped.

```
time,cpm,acpm,usv,gid
2026-09-18T21:27:53-05:00,23,15.5,0.14,01234567890
1789784918,32,15.5,0.14,01234567890
```

| Column | Required | Format |
|---|---|---|
| `time` | yes | Epoch seconds, or ISO 8601; without an offset it is UTC. |
| `cpm` | yes | Number. |
| `acpm`, `usv` | no | Number or empty. |
| `gid` | no | Counter id; empty takes the one `counter_ids` entry, if there is exactly one. |

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
