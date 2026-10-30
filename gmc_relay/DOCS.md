# GMC Relay

Receive a GQ GMC Geiger counter's WiFi upload on port 80, hand it to Home Assistant, forward it to gmcmap.com.

## Setup

1. Install the Mosquitto broker app and set up the MQTT integration Home Assistant offers for it, if they are not there already.
2. Install and start GMC Relay. No option needs setting.
3. On the counter, set Website (Menu, WiFi, Website) to this Home Assistant host's LAN address.

The first counter to upload is accepted, and a Geiger counter device with four sensors appears under the MQTT integration. The panel in the sidebar shows the readings.

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
| Check | With `counter_ids` empty, the first counter to upload is accepted and kept in `/data/counters.json`; with it set, those GIDs. Any other `GID` is refused with 403 and not forwarded. |
| Home Assistant | Published to the MQTT broker the Supervisor offers, as discovery and state; or, when `webhook_id` is set, POSTed to that webhook instead (see Webhook). |
| gmcmap | With `forward` on, the exact request is replayed to `forward_url` and the counter gets gmcmap's reply. |
| Reply | `OK.ERR0.`, gmcmap's success string, when not forwarding or when gmcmap fails. |

`GET /healthz` answers `ok` for the image health check; `GET /status.json` shows the count of readings passed on and the last one, without its `AID` and `GID`.

## Entities

One device per counter, named Geiger counter, under the MQTT integration.

| Entity | Unit | State |
|---|---|---|
| `sensor.geiger_meter_cpm` | CPM | Counts per minute. |
| `sensor.geiger_meter_acpm` | CPM | The counter's average CPM. |
| `sensor.geiger_meter_usv` | µSv/h | Dose rate. |
| `sensor.geiger_meter` | timestamp | When the reading arrived. |

A second counter gets the same ids with `_2`. The relay's status topic marks the entities unavailable while it is stopped. Topics are `homeassistant/device/gmc_relay_<key>/config` and `gmc_relay/<key>/state`, where `<key>` is a hash of the counter's `GID`; the `GID` and `AID` are not published.

## Panel

The sidebar entry GMC Relay, served on port 8099 to Home Assistant's ingress gateway only.

| Part | Shows |
|---|---|
| Header | The counter id, whether the last reading reached Home Assistant and by which route, and the last answer from gmcmap. A line under it says what is missing: the broker, or the counter's first upload. |
| Dose rate now | The latest µSv/h, CPM and ACPM, the time since the upload, and the last 60 readings' CPM. |
| Past year | Each day's average dose rate as a calendar; fewer weeks on a narrow screen. |
| Filters | Range 24 h, 7 d, 30 d, 90 d, 1 y or all; CPM or µSv/h; a counter picker when more than one counter has uploaded. |
| Tiles | Average with its change against the period before; highest reading and when; estimated dose (average µSv/h times the hours covered) with its yearly rate; reading count. |
| Trend | The average per bucket with the lowest-to-highest band; buckets of 5 min for 24 h up to 1 day for a year. A gap of more than two buckets breaks the line. Hover or arrow keys for values; Table lists every bucket. |
| Hour of day | Each local hour's average as a difference from the range's average. |
| Count distribution | How often each CPM value was read, with the Poisson distribution of the same mean. |

The status refreshes every 15 seconds and the charts every minute. Dark by default; the theme, range and measure are kept in the browser.

## History

Every reading that passes the counter check is kept in `/data/history.db` (SQLite), whether or not it reached Home Assistant; a minute's upload period adds about 25 MB a year. It is part of the app's backups.

To seed it, put `import.csv` in the app's config folder (`/addon_configs/193d9a14_gmc_relay/` on the host for this repository, `local_gmc_relay` for a local copy) and restart. It is read once and renamed `import.csv.done`; rows already in the history are skipped.

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
| `gid` | no | Counter id; empty takes the one accepted counter, if there is exactly one. |

## Options

None is needed. The rarely changed ones are under Show unused optional configuration options.

| Option | Default | Effect |
|---|---|---|
| `counter_ids` | empty | Accepted `GID` values. Empty accepts the first counter to upload. |
| `forward` | on | Replay each upload to `forward_url`. |
| `forward_url` | `https://www.gmcmap.com` | Where to replay. |
| `webhook_id` | empty | Set, readings go to this Home Assistant webhook instead of MQTT (see Webhook). Keep it private. |
| `core_url` | `https://homeassistant:8123` | Where the webhook is posted. Use `http://` if Home Assistant has no certificate. |
| `core_verify_tls` | on | Check core's certificate on an `https` `core_url`: against `core-ca.pem` in the app's config folder when it is there, else the system store. The certificate must name the `core_url` host. |
| `log_level` | info | `debug` logs every request. |

## Webhook

For a trigger-based template sensor already fed by a webhook: set `webhook_id` to its id and the relay POSTs `{"cpm", "acpm", "usv", "gid", "aid", "path", "received"}` to `core_url`/api/webhook/`webhook_id`, straight to core on the apps network, and publishes nothing to MQTT. The Supervisor's core proxy is not used: it answers 200 to a webhook POST without delivering it.

To move from that template to the MQTT entities and keep the history:

1. Remove the template sensors from Home Assistant's configuration and restart Home Assistant.
2. In Settings, Entities, delete the template entities that now show as no longer provided (`sensor.geiger_meter`, `_cpm`, `_acpm`, `_usv`), so their ids are free.
3. Clear `webhook_id` and restart GMC Relay. The MQTT sensors take the same entity ids; history and long-term statistics continue, since the units match.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Panel or log says no MQTT broker app is installed | No MQTT broker app and no `webhook_id` | Install the Mosquitto broker app; the relay finds it within 5 minutes. |
| Log says `MQTT broker ...: the broker refused the connection` | The broker turned the relay's login down | Restart GMC Relay so it asks the Supervisor for the login again. |
| No Geiger counter device | The MQTT integration is not set up | Set up the MQTT integration Home Assistant offers for the Mosquitto broker app; the device appears from the retained discovery. |
| Entity ids end in `_2` | Another entity already holds the id | Delete or rename the old entity, then rename the new one back. |
| `upload from unknown counter GID` | A second counter, or a new one replacing the first | List every counter to accept in `counter_ids` and restart. |
| `webhook refused the reading: 0 ... CERTIFICATE_VERIFY_FAILED` | Core's certificate is not signed by a CA the relay trusts, or does not name the `core_url` host | Put the CA certificate that signed core's certificate in the config folder as `core-ca.pem` and restart, or turn `core_verify_tls` off. |
| Webhook set, relay says 200 but no state | Webhook id differs from the template trigger, or the trigger is not loaded | Same id on both sides; core answers 200 even for an unknown id. |
| Nothing arrives | The counter still points at gmcmap, or has no route to this host | Check the counter's Website setting and the LAN. `status.json` counts what arrived. |
| `forward ... failed` | gmcmap unreachable or the counter id unknown there | The reading is in Home Assistant regardless. |
