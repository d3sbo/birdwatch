# BirdWatch 🐦

Real-time bird detection from your CCTV cameras using BirdNET-Analyzer and GPU acceleration.

---

## Quick Start

### 1. Configure your cameras

Copy `secrets.env.example` to `secrets.env` (it's loaded by `docker-compose.yml` and kept out of git), then set your camera URLs. go2rtc HTTP streams or Reolink RTSP URLs both work:

```ini
CAMERA_1_URL=rtsp://admin:YOUR_PASSWORD@192.168.1.100:554/h264Preview_01_main
CAMERA_1_NAME=Camera 1
```

**Reolink RTSP URL format:**
- Main stream (1080p): `rtsp://admin:PASS@IP:554/h264Preview_01_main`
- Sub stream (480p):   `rtsp://admin:PASS@IP:554/h264Preview_01_sub`

> **Tip:** Use the sub-stream if you have many cameras — BirdNET only needs audio so video resolution doesn't matter, and lower resolution means less CPU for ffmpeg.

### 2. Set your location

BirdNET uses your GPS coordinates to filter species that are actually likely in your area. Set these in `secrets.env`:

```ini
LATITUDE=54.93
LONGITUDE=-2.73
```

### 3. Set MQTT broker (for Home Assistant)

`MQTT_HOST` and `MQTT_PASSWORD` go in `secrets.env`; `MQTT_USER` is in `docker-compose.yml`:

```ini
MQTT_HOST=192.168.1.10     # Your HA server IP
MQTT_PASSWORD=mqtt_pass
```

### 4. Build and run

```bash
docker compose up -d --build
```

Dashboard available at: **http://your-server-ip:8080**

---

## GPU Setup

The container requires the NVIDIA Container Toolkit installed on the host:

```bash
# Install NVIDIA Container Toolkit (Ubuntu/Debian)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Verify GPU is visible inside the container:
```bash
docker compose run --rm birdwatch nvidia-smi
```

---

## Node-RED Setup (Home Assistant)

### Starting and stopping the container

The flow starts and stops BirdWatch by calling a small HTTP listener on the Docker host, `docker-control.ps1` (it lives in the [talking-head-ditto](https://github.com/d3sbo/talking-head-ditto/tree/main/windows) repo). Set an environment variable `DOCKER_DESKTOP` in the Node-RED add-on to its address, for example `http://192.168.1.20:2376`. The flow calls `/start-birdwatch` and `/stop-birdwatch`.

If you'd rather not run that listener, use the soft pause/resume nodes instead: they call `/api/control` on the running container and never stop it.

### Import the flow

1. Open Node-RED in Home Assistant
2. Menu → Import
3. Paste the contents of `nodered_flow.json`
4. Update the MQTT broker IP/credentials in the `mqtt-broker-config` node
5. Update the Docker container name if you changed it (default: `birdwatch`)
6. Deploy

### What the flow provides

- **Manual start/stop buttons** for the container
- **Scheduled start/stop** (default: 05:00 start, 21:30 stop)
- **MQTT listener** that turns each bird detection into a Home Assistant event
- **HA sensor** `sensor.last_bird_detected` updated on every detection
- **Soft pause/resume** via the `/api/control` REST endpoint

### Home Assistant dashboard

- `home-assistant/helpers.yaml` creates the `input_button.start_birdwatch` / `stop_birdwatch` buttons the flow listens for.
- `home-assistant/dashboard-birdwatch.yaml` has cards showing the last bird, camera, confidence and status, plus the start/stop buttons. It uses only built-in cards.
- `home-assistant/dashboard-birdwatch-retro.yaml` has the same cards in a green-on-black terminal style. It needs the `custom:button-card` card from HACS.
- `home-assistant/dashboard-birdwatch-iframe.yaml` is a whole dashboard that just embeds the BirdWatch web UI.

The `sensor.birdwatch_*` entities come from the app's MQTT discovery, so they appear on their own once MQTT is configured.

### Home Assistant automation example

After deploying the flow, you can use the `bird_detected` event in HA automations:

```yaml
# configuration.yaml or automation editor
automation:
  - alias: "Notify on rare bird"
    trigger:
      - platform: event
        event_type: bird_detected
    condition:
      - condition: template
        value_template: >
          {{ trigger.event.data.confidence | int >= 90 }}
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "🐦 Bird spotted!"
          message: >
            {{ trigger.event.data.common_name }} detected on 
            {{ trigger.event.data.camera }} 
            ({{ trigger.event.data.confidence }}% confidence)
```

---

## MQTT Topics

| Topic                  | Direction | Payload                              |
|------------------------|-----------|--------------------------------------|
| `birdwatch/detection`  | Published | JSON: `{common_name, species, confidence, camera, timestamp}` |
| `birdwatch/status`     | Published | `"running"` \| `"paused"` \| `"offline"` |
| `birdwatch/control`    | Subscribe | `"pause"` \| `"resume"` \| `"stop"` |

---

## REST API

| Endpoint                | Method | Description                        |
|-------------------------|--------|------------------------------------|
| `/`                     | GET    | Live dashboard                     |
| `/api/status`           | GET    | Container health check             |
| `/api/control`          | POST   | `{"action":"pause"\|"resume"}`    |
| `/api/detections`       | GET    | Recent detections (last 50)        |
| `/api/detections/today` | GET    | Today's detections                 |
| `/api/species`          | GET    | Species seen today with counts     |
| `/api/species/all`      | GET    | All-time species list              |
| `/api/activity`         | GET    | Hourly detection counts (today)    |

---

## Configuration Reference

| Variable              | Default    | Description                                  |
|-----------------------|------------|----------------------------------------------|
| `CAMERA_N_URL`        | required   | RTSP URL for camera N (N=1,2,3…)            |
| `CAMERA_N_NAME`       | `Camera_N` | Display name for camera N                    |
| `LATITUDE`            | `54.93`    | Your latitude (Brampton, England default)    |
| `LONGITUDE`           | `-2.73`    | Your longitude                               |
| `MIN_CONFIDENCE`      | `0.70`     | Detection threshold (raise to reduce noise)  |
| `CHUNK_SECONDS`       | `15`       | Audio chunk duration sent to BirdNET         |
| `MQTT_HOST`           | *(empty)*  | MQTT broker IP (leave empty to disable)      |
| `MQTT_PORT`           | `1883`     | MQTT broker port                             |
| `MQTT_USER`           | *(empty)*  | MQTT username                                |
| `MQTT_PASSWORD`       | *(empty)*  | MQTT password                                |
| `MQTT_TOPIC_PREFIX`   | `birdwatch`| MQTT topic namespace                         |
| `TZ`                  | `UTC`      | Container timezone                           |

---

## Data

Detections are stored in `/data/birdwatch.db` (SQLite). Mount `./data:/data` in your compose file to persist across restarts.

Temporary audio chunks are stored in `/data/audio/` and automatically cleaned up after processing.
