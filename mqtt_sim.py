"""Simulated MQTT broker with a few devices publishing, for testing without
hardware or a real broker.

Every couple of seconds it publishes:
  sim/env/room1        JSON {"temperature", "humidity", "co2"}
  sim/env/room2        JSON {"temperature", "humidity", "battery": {"voltage", "percent"}}
  sim/power/voltage    a plain number (mains volts)
  measure-station/sim-01/<source>
                       a Measure Station's readings, every source in its
                       documented format (see SimStation)
and keeps sim/status and measure-station/sim-01/status = "online" retained. It speaks just enough
MQTT 3.1.1 for subscribers: CONNECT, SUBSCRIBE (QoS 0/1), PINGREQ, DISCONNECT.

    python mqtt_sim.py            serve on 127.0.0.1:1884 until Ctrl+C
"""

import json
import math
import random
import socketserver
import struct
import threading
import time
from datetime import datetime, timezone

from mqtt_core import (CONNACK, CONNECT, DISCONNECT, PINGREQ, PINGRESP, PUBLISH, SUBACK, SUBSCRIBE,
                       PacketReader, _str, packet, parse_filters, read_str, topic_matches)

STATION = "sim-01"
STATION_BASE = "measure-station/%s/" % STATION
MQ = {"mq2": ("SMK", 13, 4.8), "mq4": ("CH4", 16, 5.4), "mq5": ("LPG", 0.85, 6.1),
      "mq7": ("CO", 0.65, 3.9), "mq8": ("H2", 53, 7.2), "mq135": ("NH3", 4.3, 8.0)}
STATION_SOURCES = ("temperature", "humidity", "barometer", "co2", "particulates", "light",
                   "air-quality") + tuple("mqboard-" + m for m in MQ) + ("health",)
TOPICS = ("sim/env/room1", "sim/env/room2", "sim/power/voltage", "sim/status") + tuple(
    STATION_BASE + s for s in STATION_SOURCES + ("status",))


class SimDevices:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.plume = 0.0
        self.battery = 100.0

    def readings(self, t):
        """[(topic, payload text)] for time t."""
        g = self.rng.gauss
        if self.rng.random() < 0.02:
            self.plume = self.rng.uniform(200, 800)
        self.plume *= 0.9
        self.battery = max(5.0, self.battery - 0.01)
        room1 = {"temperature": round(21.5 + 1.5 * math.sin(t / 600) + g(0, 0.05), 2),
                 "humidity": round(45 + 5 * math.sin(t / 900) + g(0, 0.3), 1),
                 "co2": round(420 + 30 * math.sin(t / 300) + self.plume + g(0, 5))}
        room2 = {"temperature": round(19 + 2 * math.sin(t / 700 + 1) + g(0, 0.05), 2),
                 "humidity": round(55 + 4 * math.sin(t / 800) + g(0, 0.3), 1),
                 "battery": {"voltage": round(2.7 + 0.5 * self.battery / 100, 3),
                             "percent": round(self.battery)}}
        volts = "%.1f" % (230 + 2 * math.sin(t / 120) + g(0, 0.3))
        return [("sim/env/room1", json.dumps(room1)), ("sim/env/room2", json.dumps(room2)),
                ("sim/power/voltage", volts)]


class SimStation:
    """A Measure Station publishing every source in its documented format.
    For the first hour its SEN65's VOC/NOx are flagged as warming up, for the
    first 3 minutes the ENS160 too; its MQ7 runs on a provisional baseline,
    and an MQ read occasionally fails (null)."""

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.start = time.time()

    def readings(self, t):
        g, up = self.rng.gauss, t - self.start
        ts = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")

        def msg(source, sensor_type, values, meta=None):
            d = {"station": STATION, "source": source, "sensor_type": sensor_type, "ts": ts,
                 "ts_src": "ntp", "values": values}
            if meta:
                d["meta"] = meta
            return STATION_BASE + source, json.dumps(d, separators=(",", ":"))

        temp = 18 + 3 * math.sin(t / 900) + g(0, 0.05)
        hum = 60 + 8 * math.sin(t / 1100) + g(0, 0.4)
        pm25 = max(0.0, 6 + 3 * math.sin(t / 500) + g(0, 0.5))
        lux = max(0.0, 800 + 600 * math.sin(t / 400) + g(0, 10))
        out = [
            msg("temperature", "mcp9808", {"temperature_c": round(temp * 16) / 16}),
            msg("humidity", "cht8305", {"temperature_c": round(temp + g(0, 0.1), 2),
                                        "humidity_pct": round(hum, 1)}),
            msg("barometer", "bmp280", {"pressure_hpa": round(1006.5 + 2 * math.sin(t / 3000) + g(0, 0.05), 2)},
                {"temperature_c": round(temp + 2.5, 2)}),
            msg("co2", "scd41", {"co2_ppm": round(430 + 40 * math.sin(t / 700) + g(0, 8)),
                                 "temperature_c": round(temp + 0.4, 2), "humidity_pct": round(hum - 2, 1)},
                {"pressure_hpa": 1007, "comp_src": "hpa"}),
            msg("particulates", "sen65", {
                "pm1_ugm3": round(pm25 * 0.8, 1), "pm25_ugm3": round(pm25, 1),
                "pm4_ugm3": round(pm25 * 1.1, 1), "pm10_ugm3": round(pm25 * 1.2, 1),
                "temperature_c": round(temp + 0.2, 2), "humidity_pct": round(hum - 1, 1),
                "voc_index": round(100 + 20 * math.sin(t / 600) + g(0, 3)),
                "nox_index": max(1, round(1 + 3 * math.sin(t / 800)))},
                {k: "warmup" for k, limit in (("voc", 3600), ("nox", 6 * 3600)) if up < limit}),
            msg("light", "bh1750", {"illuminance_lux": round(lux, 2)},
                {"range": "hi" if lux >= 5000 else "lo"}),
            msg("air-quality", "ens160_aht21", {
                "aqi": 2, "tvoc_ppb": round(140 + 30 * math.sin(t / 500) + g(0, 5)),
                "eco2_ppm": round(600 + 50 * math.sin(t / 500) + g(0, 8))},
                {"validity": "warmup" if up < 180 else "normal",
                 "temperature_c": round(temp + 9, 2), "humidity_pct": round(hum - 25, 1)}),
        ]
        for name, (gas, clean, r0) in MQ.items():
            ppm = clean * (1 + 0.1 * math.sin(t / 400 + r0) + g(0, 0.02))
            out.append(msg("mqboard-" + name, "mq_board",
                           {"concentration_ppm": None if self.rng.random() < 0.01 else round(ppm, 2)},
                           {"gas": gas, "temperature_c": round(temp + 1, 2),
                            "baseline_tracking": name != "mq7", "r0_kohm": r0, "ts_basis": "board"}))
        out.append(msg("health", "station", {
            "uptime_s": int(up), "outbox_backlog": self.rng.randint(0, 3), "queue_depth": 0,
            "task_restarts": 0, "clock_synced": True, "awaiting_time_sync": 0,
            "cpu_usage_pct": round(12 + 4 * math.sin(t / 300) + g(0, 1), 1),
            "cpu_temperature_c": round(48 + 2 * math.sin(t / 900) + g(0, 0.2), 2),
            "load_1m": round(0.4 + 0.1 * math.sin(t / 300), 2),
            "disk_free_mb": round(11234.5 - up / 1e4, 1),
            "mem_available_mb": round(702 + g(0, 2), 1), "ram_usage_pct": round(24 + g(0, 0.2), 1),
            "mqboard_up": True, "mqboard_last_line_age_s": round(self.rng.uniform(0, 10), 1),
            "mqboard_bad_lines": 0, "mqboard_stale_sensors": [], "mqboard_held": 0,
            "fan_failsafe": False, "fan_exhaust_duty_pct": 100, "fan_intake_duty_pct": 0,
            "fan_temp_diff_c": round(2 + 0.5 * math.sin(t / 900) + g(0, 0.05), 2),
            "fan_fog": hum > 95}))
        return out


class _Session:
    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()
        self.subs = {}              # filter -> granted QoS
        self.pid = 0

    def send(self, data):
        with self.lock:
            self.sock.sendall(data)

    def deliver(self, topic, payload, retain):
        qos = max((q for f, q in self.subs.items() if topic_matches(f, topic)), default=None)
        if qos is None:
            return
        body = _str(topic)
        if qos:
            self.pid = self.pid % 0xFFFF + 1
            body += struct.pack(">H", self.pid)
        self.send(packet(PUBLISH << 4 | qos << 1 | int(retain), body + payload))


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server, sock = self.server, self.request
        reader, session = PacketReader(), None
        try:
            while True:
                pkt = reader.next()
                if pkt is None:
                    data = sock.recv(65536)
                    if not data:
                        return
                    reader.feed(data)
                    continue
                first, body = pkt
                kind = first >> 4
                if session is None:
                    if kind != CONNECT:
                        return
                    code = server.check_connect(body)
                    sock.sendall(packet(CONNACK << 4, bytes([0, code])))
                    if code:
                        return
                    session = _Session(sock)
                    server.add(session)
                elif kind == SUBSCRIBE:
                    pid, pos, codes, new = body[:2], 2, [], []
                    while pos < len(body):
                        flt, pos = read_str(body, pos)
                        qos = min(body[pos], 1)
                        pos += 1
                        try:
                            parse_filters(flt)
                        except ValueError:
                            codes.append(0x80)
                            continue
                        session.subs[flt] = qos
                        codes.append(qos)
                        new.append(flt)
                    session.send(packet(SUBACK << 4, pid + bytes(codes)))
                    server.send_retained(session, new)
                elif kind == PINGREQ:
                    session.send(packet(PINGRESP << 4))
                elif kind == DISCONNECT:
                    return
        except (OSError, IndexError):           # connection gone, or a malformed packet
            return
        finally:
            if session is not None:
                server.remove(session)


class SimBroker(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=0, interval=2.0, devices=None, auth=None, station=None):
        """auth: (user name, password) the broker requires, or None for none."""
        self.interval = interval
        self.devices = devices or SimDevices()
        self.station = station or SimStation()
        self.auth = auth
        self.sessions = set()
        self.retained = {"sim/status": b"online", STATION_BASE + "status": b"online"}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__((host, port), _Handler)
        self.threads = [threading.Thread(target=self.serve_forever, daemon=True),
                        threading.Thread(target=self._publish_loop, daemon=True)]

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        for t in self.threads:
            t.start()
        return self

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()
        with self.lock:
            sessions = list(self.sessions)
        for s in sessions:              # unblock their handler threads
            try:
                s.sock.close()
            except OSError:
                pass

    def check_connect(self, body):
        """CONNACK return code for a CONNECT packet."""
        try:
            name, pos = read_str(body, 0)
            level, flags = body[pos], body[pos + 1]
            pos += 4                                    # level, flags, keep alive
            _client_id, pos = read_str(body, pos)
            user = pw = None
            if flags & 0x80:
                user, pos = read_str(body, pos)
            if flags & 0x40:
                pw, pos = read_str(body, pos)
        except (OSError, IndexError):
            return 2
        if name != "MQTT" or level != 4:
            return 1
        if self.auth is not None and (user, pw) != tuple(self.auth):
            return 4
        return 0

    def add(self, session):
        with self.lock:
            self.sessions.add(session)

    def remove(self, session):
        with self.lock:
            self.sessions.discard(session)

    def send_retained(self, session, filters):
        with self.lock:
            retained = list(self.retained.items())
        for topic, payload in retained:
            if any(topic_matches(f, topic) for f in filters):
                session.deliver(topic, payload, True)

    def publish(self, topic, payload, retain=False):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        with self.lock:
            if retain:
                self.retained[topic] = payload
            sessions = list(self.sessions)
        for s in sessions:
            try:
                s.deliver(topic, payload, False)    # retain flag is only for new subscriptions
            except OSError:
                pass                                # its handler cleans up

    def _publish_loop(self):
        while not self.stopping.wait(self.interval):
            now = time.time()
            for topic, payload in self.devices.readings(now) + self.station.readings(now):
                self.publish(topic, payload)


if __name__ == "__main__":
    broker = SimBroker(port=1884)
    broker.threads[1].start()
    print("Simulated MQTT broker on 127.0.0.1:%d, publishing %s (Ctrl+C to stop)"
          % (broker.port, ", ".join(TOPICS)))
    try:
        broker.serve_forever()
    except KeyboardInterrupt:
        pass
    broker.stopping.set()
    broker.server_close()
