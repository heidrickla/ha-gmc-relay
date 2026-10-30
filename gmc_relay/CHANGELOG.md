# Changelog

## 0.5.0

- Readings reach Home Assistant with no YAML: through the Mosquitto broker app, the relay publishes each counter as an MQTT discovery device with four sensors (CPM, ACPM, dose rate, last reading). It finds the broker through the Supervisor, and one installed later within 5 minutes.
- The sensors ask for the entity ids of the template sensors earlier releases documented (`sensor.geiger_meter_cpm`, `sensor.geiger_meter_usv`) and of their companions (`sensor.geiger_meter_acpm`, `sensor.geiger_meter`), with the same units, so an install that moves off the template keeps its history.
- `counter_ids` may be left empty: the first counter to upload is accepted and kept in the app's data. Set `counter_ids` to choose others.
- `webhook_id` is optional. Set, the readings go to that webhook as before and not to MQTT. `core_url`, `core_verify_tls`, `forward_url` and `log_level` are optional with the same defaults.
- The panel and the log say when the readings cannot reach Home Assistant and why, and while no counter has uploaded yet.
- The image builds from the multi-architecture base image and carries its own health check on `/healthz`. `build.yaml`, `advanced`, `watchdog` and the keys that only restated defaults are gone.

## 0.4.1

- A connection that stops sending is closed after 10 seconds on both listeners. The counter opens a second connection after each upload and never writes to it; each one used to hold a file descriptor and a thread, and after about 18 hours the relay ran out of descriptors (`Errno 24`) until the watchdog restarted it.
- A `core-ca.pem` that cannot be read is a refused webhook in the log; the counter still gets its reply and the forward still runs.

## 0.4.0

- `counter_ids` is required: while it is empty every upload is refused with 403, and the log says so once.
- Port 80's `/status.json` leaves out the last reading's `AID` and `GID`; the ingress panel keeps them.
- `core_verify_tls` (default on): core's certificate on an `https` `core_url` is checked against `core-ca.pem` in the config folder, else the system store.

## 0.3.0

- The relay keeps every accepted reading in `/data/history.db`, whatever the webhook answers.
- The panel is a dashboard: the latest dose rate with a sparkline of the last 60 readings, a daily calendar for the past year, a range filter (24 h to all) with CPM or µSv/h, average with the change against the period before, highest reading, estimated dose, the trend with each bucket's range, the hour-of-day profile and the count distribution beside a Poisson curve. Hover readouts, a table view, dark by default with a light theme.
- `import.csv` in the add-on's config folder is read once at start and renamed `import.csv.done`, to seed the history.
- An upload whose CPM is not a finite number is refused with 400.

## 0.2.0

- Status panel in the sidebar through ingress: readings passed on, the last reading, forward target and webhook state. It answers only the ingress gateway. Security rating 8.
- Base image Alpine 3.24.

## 0.1.0

- First release: port 80 listener for `log2.asp` and `log2.php`, webhook post straight to core, optional forward to gmcmap.com, counter id allow-list.
