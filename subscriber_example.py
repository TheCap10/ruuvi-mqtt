#!/usr/bin/env python3
"""
subscriber_example.py — Subscribe to ruuvi-mqtt topics and act on readings.

Copy and extend this for bots, alerting, logging to a DB, etc.
Usage: python subscriber_example.py [--config config.yaml]
"""

import argparse
import json
import logging
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Example: temperature alert thresholds (°C).
# Add your own tag MACs (lowercase, no colons) as keys.
# ---------------------------------------------------------------------------
ALERT_THRESHOLDS: dict[str, dict] = {
    # "aabbccddeeff": {"name": "Fridge Left", "min_c": 0.0, "max_c": 8.0},
}


def on_message(client: mqtt.Client, userdata: dict, msg: mqtt.MQTTMessage) -> None:
    """Called for every state message received from the broker."""
    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError:
        log.warning("Bad JSON on %s", msg.topic)
        return

    # Extract the mac from the topic: ruuvi/<mac>/state
    parts = msg.topic.split("/")
    mac = parts[1] if len(parts) >= 2 else "unknown"

    temp = data.get("temperature_c")
    hum  = data.get("humidity_rh")
    pres = data.get("pressure_hpa")
    batt = data.get("battery_mv")
    rssi = data.get("rssi")

    log.info(
        "[%s] temp=%.2f°C  hum=%.2f%%  pres=%.2fhPa  batt=%dmV  rssi=%s",
        mac, temp, hum, pres, batt, rssi,
    )

    # -----------------------------------------------------------------------
    # Example: simple threshold alerting
    # -----------------------------------------------------------------------
    if mac in ALERT_THRESHOLDS and temp is not None:
        t = ALERT_THRESHOLDS[mac]
        name = t.get("name", mac)
        if temp < t.get("min_c", float("-inf")):
            log.warning("ALERT: %s temperature %.2f°C is BELOW minimum %.1f°C!", name, temp, t["min_c"])
        elif temp > t.get("max_c", float("inf")):
            log.warning("ALERT: %s temperature %.2f°C is ABOVE maximum %.1f°C!", name, temp, t["max_c"])

    # -----------------------------------------------------------------------
    # Add your own logic here:
    #   - Post to a Discord/Telegram/Slack webhook
    #   - Write to InfluxDB / SQLite
    #   - Trigger a Home Assistant webhook
    # -----------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Ruuvi MQTT subscriber example")
    parser.add_argument("--config", "-c", default="config.yaml")
    args = parser.parse_args()

    cfg: dict = {}
    p = Path(args.config)
    if p.exists():
        with p.open() as fh:
            cfg = yaml.safe_load(fh) or {}

    mc = cfg.get("mqtt", {})
    host   = mc.get("host", "localhost")
    port   = int(mc.get("port", 1883))
    prefix = mc.get("topic_prefix", "ruuvi")
    user   = mc.get("username")
    pw     = mc.get("password")

    client = mqtt.Client(client_id="ruuvi-subscriber", protocol=mqtt.MQTTv5)
    if user:
        client.username_pw_set(user, pw)

    def on_connect(c, userdata, flags, rc, props=None):
        if rc == 0:
            topic = f"{prefix}/+/state"
            c.subscribe(topic)
            log.info("Connected to %s:%s — subscribed to %s", host, port, topic)
        else:
            log.error("Connect failed rc=%s", rc)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    client.connect(host, port, keepalive=60)

    log.info("Listening for Ruuvi readings. Ctrl-C to stop.")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
