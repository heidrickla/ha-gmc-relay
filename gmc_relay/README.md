# GMC Relay

Receive a GQ GMC Geiger counter's WiFi upload on port 80, turn it into Home Assistant sensors through MQTT discovery, and forward it to gmcmap.com so the public map keeps updating. A sidebar panel graphs the history it keeps: dose rate now, a year's calendar, trends, hour of day and the count distribution. Needs the Mosquitto broker app; no option needs setting. Point the counter's Website setting at the host. See the Documentation tab.
