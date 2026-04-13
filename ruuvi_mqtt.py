#!/usr/bin/env python3
"""
ruuvi_mqtt.py — Scan for Ruuvi BLE tags and publish to MQTT with
Home Assistant MQTT Discovery support.

Requires Python 3.9+ and the packages in requirements.txt.
Run with: python ruuvi_mqtt.py [--config config.yaml]
"""

import argparse
import asyncio
import json
import logging
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
import yaml
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RUUVI_MANUFACTURER_ID = 0x0499  # Ruuvi Innovations company ID
DATA_FORMAT_5 = 0x05            # RAWv2

# Struct layout for Ruuvi DF5 manufacturer payload (big-endian, 24 bytes total)
#   B  = data_format (1)
#   h  = temperature raw (2, signed)
#   H  = humidity raw   (2)
#   H  = pressure raw   (2)
#   h  = accel X        (2, signed)
#   h  = accel Y        (2, signed)
#   h  = accel Z        (2, signed)
#   H  = power info     (2)
#   B  = movement cnt   (1)
#   H  = sequence       (2)
#   ---                 18 bytes; bytes 18-23 = MAC (unused here — we use BLE addr)
DF5_STRUCT = struct.Struct(">BhHHhhhHBH")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RuuviMeasurement:
    mac: str               # BLE address, e.g. "AA:BB:CC:DD:EE:FF"
    temperature_c: float   # degrees Celsius
    humidity_rh: float     # relative humidity %
    pressure_hpa: float    # hPa
    accel_x_mg: int        # milli-g
    accel_y_mg: int
    accel_z_mg: int
    battery_mv: int        # millivolts
    tx_power_dbm: int
    movement_counter: int
    measurement_sequence: int
    rssi: Optional[int]
    timestamp: float


# ---------------------------------------------------------------------------
# Ruuvi parsing
# ---------------------------------------------------------------------------

def parse_df5(payload: bytes, mac: str, rssi: Optional[int]) -> Optional[RuuviMeasurement]:
    """Parse a Ruuvi Data Format 5 manufacturer payload into a measurement."""
    if len(payload) < 24 or payload[0] != DATA_FORMAT_5:
        return None
    try:
        (_, temp, hum, pres, ax, ay, az, pwr, mov, seq) = DF5_STRUCT.unpack_from(payload)
    except struct.error as exc:
        log.debug("DF5 unpack failed for %s: %s", mac, exc)
        return None

    return RuuviMeasurement(
        mac=mac,
        temperature_c=round(temp * 0.005, 3),
        humidity_rh=round(hum * 0.0025, 4),
        pressure_hpa=round((pres + 50000) / 100.0, 2),
        accel_x_mg=ax,
        accel_y_mg=ay,
        accel_z_mg=az,
        battery_mv=(pwr >> 5) + 1600,
        tx_power_dbm=(pwr & 0x1F) * 2 - 40,
        movement_counter=mov,
        measurement_sequence=seq,
        rssi=rssi,
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "username": None,
        "password": None,
        "topic_prefix": "ruuvi",
        "client_id": "ruuvi-mqtt",
    },
    "scan": {
        "publish_interval": 10,   # minimum seconds between publishes per tag
    },
    "ha_discovery": {
        "enabled": True,
        "prefix": "homeassistant",
    },
    "tags": [],   # list of {mac, name}; empty = accept all Ruuvi tags
    "log_level": "INFO",
}


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open() as fh:
            user_cfg = yaml.safe_load(fh) or {}
        cfg = deep_merge(cfg, user_cfg)
    else:
        log.warning("Config file %s not found; using defaults.", path)
    return cfg


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

SENSOR_DEFS = [
    # (key_in_payload, unit, device_class, state_class, icon)
    ("temperature_c",        "°C",   "temperature",  "measurement", None),
    ("humidity_rh",          "%",    "humidity",     "measurement", None),
    ("pressure_hpa",         "hPa",  "pressure",     "measurement", None),
    ("battery_mv",           "mV",   "voltage",      "measurement", None),
    ("rssi",                 "dBm",  "signal_strength", "measurement", None),
    ("movement_counter",     None,   None,           "total_increasing", "mdi:run"),
    ("accel_x_mg",           "mg",   None,           "measurement", "mdi:axis-x-arrow"),
    ("accel_y_mg",           "mg",   None,           "measurement", "mdi:axis-y-arrow"),
    ("accel_z_mg",           "mg",   None,           "measurement", "mdi:axis-z-arrow"),
]

SENSOR_NAMES = {
    "temperature_c":    "Temperature",
    "humidity_rh":      "Humidity",
    "pressure_hpa":     "Pressure",
    "battery_mv":       "Battery Voltage",
    "rssi":             "Signal Strength",
    "movement_counter": "Movement Counter",
    "accel_x_mg":       "Acceleration X",
    "accel_y_mg":       "Acceleration Y",
    "accel_z_mg":       "Acceleration Z",
}


def mac_to_id(mac: str) -> str:
    """Convert 'AA:BB:CC:DD:EE:FF' → 'aabbccddeeff'."""
    return mac.replace(":", "").lower()


def publish_ha_discovery(client: mqtt.Client, mac: str, tag_name: str, cfg: dict) -> None:
    """Publish Home Assistant MQTT discovery configs for all sensors of one tag."""
    ha_prefix = cfg["ha_discovery"]["prefix"]
    topic_prefix = cfg["mqtt"]["topic_prefix"]
    device_id = mac_to_id(mac)
    state_topic = f"{topic_prefix}/{device_id}/state"

    device_info = {
        "identifiers": [f"ruuvi_{device_id}"],
        "name": tag_name,
        "manufacturer": "Ruuvi Innovations",
        "model": "RuuviTag",
    }

    for key, unit, device_class, state_class, icon in SENSOR_DEFS:
        object_id = f"{device_id}_{key}"
        unique_id = f"ruuvi_{object_id}"
        sensor_name = SENSOR_NAMES[key]

        payload: dict = {
            "name": sensor_name,
            "unique_id": unique_id,
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "state_class": state_class,
            "device": device_info,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if icon:
            payload["icon"] = icon

        discovery_topic = f"{ha_prefix}/sensor/{device_id}/{key}/config"
        client.publish(discovery_topic, json.dumps(payload), retain=True)
        log.debug("Published HA discovery: %s", discovery_topic)

    log.info("Published HA discovery for %s (%s)", tag_name, mac)


def publish_measurement(client: mqtt.Client, m: RuuviMeasurement, cfg: dict) -> None:
    """Publish a sensor state JSON payload."""
    topic_prefix = cfg["mqtt"]["topic_prefix"]
    device_id = mac_to_id(m.mac)
    state_topic = f"{topic_prefix}/{device_id}/state"

    payload = {
        "temperature_c":        m.temperature_c,
        "humidity_rh":          m.humidity_rh,
        "pressure_hpa":         m.pressure_hpa,
        "battery_mv":           m.battery_mv,
        "tx_power_dbm":         m.tx_power_dbm,
        "rssi":                 m.rssi,
        "accel_x_mg":           m.accel_x_mg,
        "accel_y_mg":           m.accel_y_mg,
        "accel_z_mg":           m.accel_z_mg,
        "movement_counter":     m.movement_counter,
        "measurement_sequence": m.measurement_sequence,
        "timestamp":            m.timestamp,
    }
    client.publish(state_topic, json.dumps(payload))
    log.debug("Published state to %s: %s", state_topic, payload)


# ---------------------------------------------------------------------------
# MQTT client setup
# ---------------------------------------------------------------------------

def make_mqtt_client(cfg: dict) -> mqtt.Client:
    mc = cfg["mqtt"]
    client = mqtt.Client(client_id=mc["client_id"], protocol=mqtt.MQTTv5)

    if mc.get("username"):
        client.username_pw_set(mc["username"], mc.get("password"))

    def on_connect(c, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("MQTT connected to %s:%s", mc["host"], mc["port"])
        else:
            log.error("MQTT connect failed, rc=%s", rc)

    def on_disconnect(c, userdata, rc, props=None):
        if rc != 0:
            log.warning("MQTT unexpected disconnect rc=%s; will auto-reconnect", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=2, max_delay=60)

    client.connect(mc["host"], mc["port"], keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------------------
# Main BLE scanning loop
# ---------------------------------------------------------------------------

async def run_scanner(cfg: dict, client: mqtt.Client) -> None:
    publish_interval: float = cfg["scan"]["publish_interval"]

    # Build mac→name map from config (upper-cased for consistent comparison)
    tag_map: dict[str, str] = {
        t["mac"].upper(): t["name"]
        for t in cfg.get("tags", [])
    }
    filter_macs = bool(tag_map)   # if tags configured, only accept those

    # Track last publish time per MAC to throttle output
    last_published: dict[str, float] = {}

    # Publish HA discovery for pre-configured tags immediately
    if cfg["ha_discovery"]["enabled"]:
        for mac, name in tag_map.items():
            publish_ha_discovery(client, mac, name, cfg)

    discovery_done: set[str] = set(tag_map.keys())

    def detection_callback(device: BLEDevice, adv: AdvertisementData) -> None:
        mfr_data = adv.manufacturer_data
        if RUUVI_MANUFACTURER_ID not in mfr_data:
            return

        mac = device.address.upper()
        if filter_macs and mac not in tag_map:
            return

        payload = mfr_data[RUUVI_MANUFACTURER_ID]
        measurement = parse_df5(payload, mac, adv.rssi)
        if measurement is None:
            log.debug("Non-DF5 Ruuvi packet from %s (format byte=%02X)", mac, payload[0] if payload else -1)
            return

        now = time.monotonic()
        if now - last_published.get(mac, 0) < publish_interval:
            return   # throttled
        last_published[mac] = now

        # Auto-discover unknown tags (when no filter configured)
        if mac not in discovery_done and cfg["ha_discovery"]["enabled"]:
            name = tag_map.get(mac, f"RuuviTag {mac[-5:]}")
            publish_ha_discovery(client, mac, name, cfg)
            discovery_done.add(mac)

        tag_name = tag_map.get(mac, mac)
        log.info(
            "[%s] temp=%.2f°C  hum=%.2f%%  pres=%.2fhPa  batt=%dmV  rssi=%s",
            tag_name,
            measurement.temperature_c,
            measurement.humidity_rh,
            measurement.pressure_hpa,
            measurement.battery_mv,
            measurement.rssi,
        )
        publish_measurement(client, measurement, cfg)

    log.info(
        "Starting BLE scanner (publish_interval=%ss, filter=%s)",
        publish_interval,
        list(tag_map.keys()) if filter_macs else "all Ruuvi tags",
    )

    async with BleakScanner(detection_callback=detection_callback):
        # Scan indefinitely; Ctrl-C or SIGTERM will cancel the task
        while True:
            await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Ruuvi BLE → MQTT publisher")
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    client = make_mqtt_client(cfg)

    try:
        asyncio.run(run_scanner(cfg, client))
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
