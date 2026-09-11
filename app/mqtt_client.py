"""
mqtt_client.py  –  Optional MQTT integration.
Publishes detections to HA and subscribes to control commands from Node-RED.

Topics published:
  {prefix}/detection          – JSON payload for each bird detected
  {prefix}/status             – "running" | "paused"

Topics subscribed:
  {prefix}/control            – "pause" | "resume" | "stop"
"""

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


class MQTTClient:
    def __init__(self, host: str, port: int, user: str, password: str, topic_prefix: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.prefix = topic_prefix.rstrip("/")
        self._client = None
        self._connected = False
        self._callbacks: dict[str, list] = {}

    def on_command(self, cb):
        """Register a callback for control commands: cb(command: str)"""
        self._callbacks.setdefault("command", []).append(cb)

    def start(self):
        if not MQTT_AVAILABLE:
            logger.warning("paho-mqtt not installed, MQTT disabled")
            return

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.user:
            self._client.username_pw_set(self.user, self.password)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        thread = threading.Thread(target=self._connect_loop, daemon=True, name="mqtt")
        thread.start()

    def _connect_loop(self):
        """Retry connection indefinitely."""
        while True:
            try:
                logger.info(f"Connecting to MQTT broker at {self.host}:{self.port}…")
                self._client.connect(self.host, self.port, keepalive=60)
                self._client.loop_forever()
            except Exception as e:
                logger.warning(f"MQTT connection failed: {e}. Retrying in 30 s…")
                time.sleep(30)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self._connected = True
            logger.info("✅ MQTT connected")
            client.subscribe(f"{self.prefix}/control")
            self._publish_discovery()
            self.publish_status("running")
        else:
            logger.warning(f"MQTT connect refused: {reason_code}")

    def _publish_discovery(self):
        """Publish HA MQTT discovery so BirdWatch appears as a device."""
        device = {
            "identifiers": ["birdwatch"],
            "name": "BirdWatch",
            "model": "BirdWatch Bird Detector",
            "manufacturer": "BirdWatch",
            "sw_version": "1.0.0",
        }

        sensors = [
            {
                "id": "last_bird",
                "name": "Last Bird Detected",
                "icon": "mdi:bird",
                "value_template": "{{ value_json.common_name }}",
                "state_topic": f"{self.prefix}/detection",
                "attributes": True,
            },
            {
                "id": "last_confidence",
                "name": "Last Detection Confidence",
                "icon": "mdi:percent",
                "value_template": "{{ (value_json.confidence * 100) | round }}",
                "unit": "%",
                "state_topic": f"{self.prefix}/detection",
            },
            {
                "id": "last_camera",
                "name": "Last Detection Camera",
                "icon": "mdi:cctv",
                "value_template": "{{ value_json.camera }}",
                "state_topic": f"{self.prefix}/detection",
            },
            {
                "id": "status",
                "name": "BirdWatch Status",
                "icon": "mdi:eye",
                "value_template": "{{ value }}",
                "state_topic": f"{self.prefix}/status",
            },
        ]

        for s in sensors:
            uid = "birdwatch_" + s["id"]
            payload = {
                "name": s["name"],
                "unique_id": uid,
                "icon": s["icon"],
                "state_topic": s["state_topic"],
                "value_template": s["value_template"],
                "device": device,
                "availability_topic": f"{self.prefix}/status",
                "payload_available": "running",
                "payload_not_available": "stopped",
            }
            if "unit" in s:
                payload["unit_of_measurement"] = s["unit"]
            if s.get("attributes"):
                payload["json_attributes_topic"] = s["state_topic"]
                payload["json_attributes_template"] = (
                    "{{ {'species': value_json.species,"
                    " 'confidence': ((value_json.confidence * 100) | round | string) + '%',"
                    " 'camera': value_json.camera,"
                    " 'last_seen': value_json.timestamp} | tojson }}"
                )

            self._client.publish(
                f"homeassistant/sensor/{uid}/config",
                json.dumps(payload),
                qos=1,
                retain=True,
            )
            logger.debug(f"📡 HA discovery: {uid}")

        logger.info("✅ Home Assistant MQTT discovery published")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self._connected = False
        logger.warning(f"MQTT disconnected (rc={reason_code})")

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip().lower()
        logger.info(f"MQTT command received: {payload}")
        for cb in self._callbacks.get("command", []):
            try:
                cb(payload)
            except Exception as e:
                logger.error(f"MQTT command callback error: {e}")

    def publish_detection(self, detection: dict):
        if not self._connected:
            return
        try:
            self._client.publish(
                f"{self.prefix}/detection",
                json.dumps(detection),
                qos=0,
                retain=False,
            )
        except Exception as e:
            logger.error(f"MQTT publish error: {e}")

    def publish_status(self, status: str):
        if not self._connected:
            return
        try:
            self._client.publish(f"{self.prefix}/status", status, qos=1, retain=True)
        except Exception as e:
            logger.error(f"MQTT status publish error: {e}")
