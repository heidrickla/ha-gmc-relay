# Changelog

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
