# ruuvi-mqtt

Scans for [Ruuvi](https://ruuvi.com/) BLE sensor tags and publishes readings
to an MQTT broker. Includes automatic **Home Assistant MQTT Discovery** so
sensors appear in HA without any manual YAML configuration.

Tested on Raspberry Pi OS (Bookworm) with Python 3.11.

## Sensors published per tag

| Sensor | Unit |
|---|---|
| Temperature | °C |
| Humidity | % RH |
| Pressure | hPa |
| Battery Voltage | mV |
| Signal Strength (RSSI) | dBm |
| Movement Counter | — |
| Acceleration X / Y / Z | mg |

## Quick start

```bash
# 1. Clone and enter the repo
git clone https://github.com/thecap10/ruuvi-mqtt.git
cd ruuvi-mqtt

# 2. Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp config.example.yaml config.yaml
nano config.yaml          # set your broker IP, tag MACs, and friendly names

# 4. Find your Ruuvi tag MAC addresses (optional – script discovers all tags if omitted)
#    Power cycle a tag near the Pi, then:
sudo hcitool lescan      # Ctrl-C after a few seconds; look for "Ruuvi" entries

# 5. Run
python ruuvi_mqtt.py
```

## Running as a systemd service (Pi)

```bash
# Copy files to home directory
cp -r . /home/pi/ruuvi-mqtt

# Install the service
sudo cp ruuvi-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ruuvi-mqtt
sudo systemctl start ruuvi-mqtt

# Check logs
journalctl -u ruuvi-mqtt -f
```

> **BLE permissions**: The service unit grants `CAP_NET_RAW` / `CAP_NET_ADMIN`
> so it runs as the `pi` user without needing `sudo`. If you run the script
> manually and get a Bluetooth permission error, prefix with `sudo` or add your
> user to the `bluetooth` group.

## MQTT topics

| Purpose | Topic |
|---|---|
| Sensor state (JSON) | `ruuvi/<mac>/state` |
| HA discovery config | `homeassistant/sensor/<mac>_<field>/config` |

State payload example:
```json
{
  "temperature_c": 4.12,
  "humidity_rh": 68.5,
  "pressure_hpa": 1013.25,
  "battery_mv": 2950,
  "tx_power_dbm": 4,
  "rssi": -72,
  "accel_x_mg": -8,
  "accel_y_mg": 2,
  "accel_z_mg": 1008,
  "movement_counter": 14,
  "measurement_sequence": 2041,
  "timestamp": 1712345678.9
}
```

## Home Assistant

If `ha_discovery.enabled` is `true`, sensors are created automatically under
**Settings → Devices & Services → MQTT** the first time a reading is published.
Each Ruuvi tag appears as a single HA *device* with all its sensors grouped
together.

Make sure your HA MQTT integration is configured with the same broker and that
the discovery prefix matches (`homeassistant` by default).

## Configuration reference

See [`config.example.yaml`](config.example.yaml) for all options with comments.

| Key | Default | Description |
|---|---|---|
| `mqtt.host` | `localhost` | MQTT broker hostname or IP |
| `mqtt.port` | `1883` | Broker port |
| `mqtt.username` / `mqtt.password` | — | Optional broker credentials |
| `mqtt.topic_prefix` | `ruuvi` | Root of published topics |
| `scan.publish_interval` | `10` | Seconds between publishes per tag |
| `ha_discovery.enabled` | `true` | Publish HA discovery configs |
| `ha_discovery.prefix` | `homeassistant` | Must match HA's discovery prefix |
| `tags` | `[]` | MAC + name list; empty = all Ruuvi tags |
| `log_level` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
