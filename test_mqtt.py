"""Tests for the MQTT client, topic filters, payload parsing, subscriber and
settings, run against the simulated broker over real sockets.
Run: python -m unittest -v"""

import csv
import json
import os
import socket
import tempfile
import threading
import time
import unittest

from mqtt_core import (MqttClient, MqttCsvLogger, MqttError, MqttSubscriber, PacketReader, is_mqtt_log,
                       load_settings, numeric_fields, packet, parse_filters, parse_reading, parse_time,
                       payload_text, read_mqtt_logs, reading_fields, save_settings, series_label,
                       topic_matches)
from mqtt_sim import STATION_SOURCES, TOPICS, SimBroker, SimDevices, SimStation
from station_spec import (MQ_SOURCES, SENSOR_TABS, assess, details, is_large, label, line_gap,
                          shared_keys, source_title, unit)


class TopicTests(unittest.TestCase):
    def test_matches(self):
        yes = [("#", "a/b"), ("a/#", "a"), ("a/#", "a/b/c"), ("a/+/c", "a/b/c"), ("+/+", "a/b"),
               ("+/a", "/a"), ("a/b", "a/b"), ("$SYS/#", "$SYS/load")]
        no = [("a/+", "a/b/c"), ("a/b", "a/b/c"), ("a/+/c", "a/b/d"), ("+", "a/b"),
              ("#", "$SYS/load"), ("+/load", "$SYS/load"), ("a/b/c", "a/b")]
        for f, t in yes:
            self.assertTrue(topic_matches(f, t), (f, t))
        for f, t in no:
            self.assertFalse(topic_matches(f, t), (f, t))

    def test_parse_filters(self):
        self.assertEqual(parse_filters(" home/#, sensors/+/temp ,"), ["home/#", "sensors/+/temp"])
        for bad in ("", " , ", "a/#/b", "a/b#", "a/x+/c"):
            with self.assertRaises(ValueError, msg=bad):
                parse_filters(bad)


class PayloadTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(numeric_fields("23.5"), {"": 23.5})
        self.assertEqual(numeric_fields(" -4 \n"), {"": -4.0})
        self.assertEqual(numeric_fields('"7"'), {"": 7.0})
        for none in ("", "ON", "23.5 C", "NaN", "inf", "null", "hex:00ff"):
            self.assertEqual(numeric_fields(none), {}, none)

    def test_json(self):
        text = json.dumps({"temp": 21.3, "env": {"hum": "45", "ok": True, "name": "x"},
                           "list": [1, {"v": 2}], "bad": None})
        self.assertEqual(numeric_fields(text), {"temp": 21.3, "env.hum": 45.0, "env.ok": 1.0,
                                                "list.0": 1.0, "list.1.v": 2.0})
        self.assertEqual(len(numeric_fields(json.dumps(list(range(500))))), 64)   # capped

    def test_text_and_labels(self):
        self.assertEqual(payload_text("21 \N{DEGREE SIGN}C".encode("utf-8")), "21 \N{DEGREE SIGN}C")
        self.assertEqual(payload_text(b"\xff\x00"), "hex:ff00")
        self.assertEqual(series_label("a/b", ""), "a/b")
        self.assertEqual(series_label("a/b", "env.temp"), "a/b : env.temp")


class PacketTests(unittest.TestCase):
    def test_reader_any_chunking(self):
        big = b"x" * 20000                          # 3-byte remaining length
        stream = packet(0x30, b"abc") + packet(0xD0) + packet(0x30, big)
        for size in (1, 2, 7, len(stream)):
            r, got = PacketReader(), []
            for i in range(0, len(stream), size):
                r.feed(stream[i:i + size])
                while (p := r.next()) is not None:
                    got.append(p)
            self.assertEqual(got, [(0x30, b"abc"), (0xD0, b""), (0x30, big)], size)

    def test_reader_bad_length(self):
        r = PacketReader()
        r.feed(b"\x30\xff\xff\xff\xff\x01")
        with self.assertRaises(OSError):
            r.next()


class BrokerTestCase(unittest.TestCase):
    auth = None

    def setUp(self):
        self.broker = SimBroker(interval=0.05, devices=SimDevices(seed=1), auth=self.auth).start()
        self.addCleanup(self.broker.stop)

    def receive(self, client, n, timeout=5):
        msgs, end = [], time.monotonic() + timeout
        while len(msgs) < n and time.monotonic() < end:
            msgs += client.receive(0.2)
        return msgs


class ClientTests(BrokerTestCase):
    def test_subscribe_and_receive(self):
        c = MqttClient("127.0.0.1", self.broker.port)
        self.addCleanup(c.close)
        self.assertEqual(c.subscribe(["sim/env/#", "sim/status"], qos=1), [1, 1])
        msgs = self.receive(c, 10)
        topics = {m.topic for m in msgs}
        self.assertEqual(topics, {"sim/env/room1", "sim/env/room2", "sim/status"})
        status = [m for m in msgs if m.topic == "sim/status"]
        self.assertEqual([(m.payload, m.retain) for m in status], [(b"online", True)])
        room2 = next(m for m in msgs if m.topic == "sim/env/room2")
        self.assertFalse(room2.retain)
        self.assertIn("battery.voltage", numeric_fields(payload_text(room2.payload)))

    def test_bad_filter_refused(self):
        c = MqttClient("127.0.0.1", self.broker.port)
        self.addCleanup(c.close)
        self.assertEqual(c.subscribe(["a/#/b", "sim/power/voltage"]), [0x80, 0])
        msgs = self.receive(c, 2)
        self.assertTrue(msgs and all(m.topic == "sim/power/voltage" for m in msgs))


class AuthTests(BrokerTestCase):
    auth = ("user", "secret")

    def test_credentials(self):
        with self.assertRaises(MqttError) as cm:
            MqttClient("127.0.0.1", self.broker.port, "user", "wrong")
        self.assertEqual(cm.exception.code, 4)
        with self.assertRaises(MqttError):
            MqttClient("127.0.0.1", self.broker.port)
        MqttClient("127.0.0.1", self.broker.port, "user", "secret").close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                       # nothing listens here now
    return port


class Collector:
    def __init__(self, want):
        self.events, self.want, self.done = [], want, threading.Event()

    def __call__(self, kind, payload):
        self.events.append((kind, payload))
        if self.want(self):
            self.done.set()

    def of(self, kind):
        return [p for k, p in self.events if k == kind]


class SubscriberTests(BrokerTestCase):
    def test_messages_and_csv(self):
        events = Collector(lambda c: len(c.of("message")) >= 6)
        with tempfile.TemporaryDirectory() as d:
            sub = MqttSubscriber("127.0.0.1", self.broker.port, ["#"], 0, events, d)
            sub.start()
            events.done.wait(10)
            sub.stop()
            with open(sub.csv.path, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        msgs = events.of("message")
        self.assertGreaterEqual(len(msgs), 6)
        self.assertEqual(rows[0], ["pc_time", "topic", "retained", "payload"])
        self.assertEqual(len(rows), len(msgs) + 1)
        self.assertEqual(rows[1][1:], ["sim/status", "1", "online"])     # retained comes first
        volts = next(m for m in msgs if m[1] == "sim/power/voltage")
        self.assertTrue(225 < volts[3][""] < 235, volts)
        self.assertEqual(sub.failed, 0)
        self.assertEqual({m[1] for m in msgs} - set(TOPICS), set())

    def test_no_broker_then_broker(self):
        port = free_port()
        events = Collector(lambda c: len(c.of("message")) >= 1)
        with tempfile.TemporaryDirectory() as d:
            sub = MqttSubscriber("127.0.0.1", port, ["sim/#"], 0, events, d, retry=0.1)
            sub.start()
            deadline = time.monotonic() + 10    # Windows takes ~2 s to refuse a connection
            while sub.failed < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(sub.connected)
            broker = SimBroker(port=port, interval=0.05).start()      # broker comes up
            self.addCleanup(broker.stop)
            events.done.wait(10)
            sub.stop()
        errors = [t for lv, t in events.of("log") if lv == "error"]
        self.assertEqual(len(errors), 1, errors)                       # reported once, not per retry
        self.assertIn("cannot connect", errors[0])
        self.assertTrue(events.of("message"))
        self.assertGreater(sub.failed, 1)

    def test_reconnects_after_broker_restart(self):
        port = free_port()
        broker = SimBroker(port=port, interval=0.05).start()
        events = Collector(lambda c: sum(t.startswith("connected") for lv, t in c.of("log")) >= 2)
        with tempfile.TemporaryDirectory() as d:
            sub = MqttSubscriber("127.0.0.1", port, ["sim/#"], 0, events, d, retry=0.1)
            sub.start()
            deadline = time.monotonic() + 5
            while not sub.messages and time.monotonic() < deadline:
                time.sleep(0.05)
            broker.stop()
            broker = SimBroker(port=port, interval=0.05).start()
            self.addCleanup(broker.stop)
            events.done.wait(10)
            sub.stop()
        logs = [t for lv, t in events.of("log")]
        self.assertTrue(any("lost the connection" in t for t in logs), logs)
        self.assertEqual(sum(t.startswith("connected") for t in logs), 2, logs)


CO2 = ('{"station":"mstn-2b","source":"co2","sensor_type":"scd41",'
       '"ts":"2026-10-21T09:40:02.517+00:00","ts_src":"ntp",'
       '"values":{"co2_ppm":500,"temperature_c":25.0,"humidity_pct":37.0},'
       '"meta":{"pressure_hpa":1007,"comp_src":"hpa"}}')


class StationReadingTests(unittest.TestCase):
    def test_parse_reading(self):
        r = parse_reading(CO2)
        self.assertEqual((r["station"], r["source"], r["sensor_type"], r["ts_src"]),
                         ("mstn-2b", "co2", "scd41", "ntp"))
        self.assertAlmostEqual(r["ts"], 1792575602.517, places=3)
        self.assertEqual(reading_fields(r), {"values.co2_ppm": 500.0, "values.temperature_c": 25.0,
                                             "values.humidity_pct": 37.0, "meta.pressure_hpa": 1007.0})
        for other in ("23.5", "online", '{"temp": 1}', '{"station":"a","source":"b","values":3}', "{bad"):
            self.assertIsNone(parse_reading(other), other)

    def test_nulls_and_missing_meta(self):
        r = parse_reading('{"station":"s","source":"mqboard-mq4","ts":"bad","values":'
                          '{"concentration_ppm":null},"meta":{"baseline_tracking":true}}')
        self.assertIsNone(r["ts"])                              # unparseable: arrival time is used
        self.assertEqual(reading_fields(r), {"values.concentration_ppm": None})
        r = parse_reading('{"station":"s","source":"temperature","values":{"temperature_c":1}}')
        self.assertEqual(r["meta"], {})

    def test_parse_time(self):
        self.assertEqual(parse_time("2026-10-21T00:00:00.000+00:00"), 1792540800.0)
        self.assertEqual(parse_time("2026-10-21T00:00:00Z"), 1792540800.0)
        self.assertIsNone(parse_time("2026-10-21T00:00:00"))   # no offset: ambiguous
        self.assertIsNone(parse_time(None))

    def test_names_and_units(self):
        self.assertEqual((label("pm25_ugm3"), unit("pm25_ugm3")), ("PM2.5", "\N{MICRO SIGN}g/m\N{SUPERSCRIPT THREE}"))
        self.assertEqual(unit("humidity_pct"), "%RH")
        self.assertEqual(unit("ram_usage_pct"), "%")
        self.assertEqual((label("new_thing_kohm"), unit("new_thing_kohm")), ("New thing", "k\N{OHM SIGN}"))
        self.assertEqual(unit("uptime_s"), "h")
        self.assertEqual(source_title("mqboard-mq135", {"meta": {"gas": "NH3"}}), "MQ135 NH3")
        self.assertTrue(is_large("co2", "values", "co2_ppm"))
        self.assertFalse(is_large("co2", "meta", "pressure_hpa"))
        self.assertTrue(is_large("health", "values", "cpu_usage_pct"))
        self.assertFalse(is_large("health", "values", "uptime_s"))

    def test_details(self):
        r = parse_reading('{"station":"s","source":"health","values":{"uptime_s":5,"clock_synced":true,'
                          '"cpu_usage_pct":null,"fan_fog":null,"mqboard_stale_sensors":["MQ4"]}}')
        self.assertEqual(details(r, {"values": ["uptime_s", "cpu_usage_pct"]}),
                         "clock_synced: yes   fan_fog: unknown   mqboard_stale_sensors: MQ4")

    def test_assess(self):
        def check(source, values, meta=None, age=10, ts_src="ntp", **kw):
            return assess(source, {"values": values, "meta": meta or {}, "ts_src": ts_src}, age, 60, **kw)

        self.assertEqual(check("co2", {"co2_ppm": 500}, {"comp_src": "hpa"}), ("ok", "ok"))
        self.assertEqual(check("co2", {"co2_ppm": 500}, age=400)[0], "warn")      # stale
        self.assertEqual(check("temperature", {}, ts_src="estimated")[0], "warn")
        self.assertEqual(check("air-quality", {}, {"validity": "warmup"})[0], "warn")
        self.assertEqual(check("air-quality", {}, {"validity": "invalid"})[0], "bad")
        self.assertEqual(check("particulates", {}, {"pm": "fan_speed"})[0], "bad")
        self.assertEqual(check("mqboard-mq4", {"concentration_ppm": None})[0], "bad")
        self.assertIn("placeholder", check("mqboard-mq4", {"concentration_ppm": 1},
                                           {"baseline_tracking": False, "r0_kohm": 10.0})[1])
        self.assertEqual(check("mqboard-mq7", {"concentration_ppm": 1},
                               {"baseline_tracking": False, "r0_kohm": 3.9})[0], "warn")
        self.assertEqual(check("health", {"clock_synced": False})[0], "bad")
        self.assertEqual(check("health", {"fan_intake_duty_pct": 75})[0], "warn")
        self.assertEqual(check("health", {}, station_status="offline")[0], "bad")
        self.assertEqual(check("health", {"fan_fog": True}), ("ok", "fog mode"))

    def test_line_gap(self):
        # Regression: a short series must use its real interval (MQ: 150 s),
        # not compare each time with itself and fall back to the 30 s floor.
        self.assertEqual(line_gap([0, 150, 300, 450, 600]), 450)
        self.assertEqual(line_gap([0, 145.1, 295.1]), 3 * 147.55)
        self.assertEqual(line_gap([0, 10, 20, 30]), 30)             # floor
        self.assertEqual(line_gap([5]), 30)
        self.assertEqual(line_gap([]), 30)
        self.assertEqual(line_gap([0, 60, 60, 120]), 180)           # equal times are skipped
        self.assertEqual(line_gap(list(range(0, 100 * 60, 60)) + [10000]), 180)   # recent 50 only

    def test_shared_mq_temperature(self):
        self.assertEqual(shared_keys(MQ_SOURCES), {("meta", "temperature_c"): "Board temp"})
        self.assertEqual(shared_keys(("co2",)), {})

    def test_sim_station_follows_the_spec(self):
        msgs = SimStation(seed=1).readings(time.time())
        self.assertEqual([t.rsplit("/", 1)[1] for t, _ in msgs], list(STATION_SOURCES))
        for topic, payload in msgs:
            r = parse_reading(payload)
            self.assertIsNotNone(r, topic)
            self.assertEqual(topic, "measure-station/sim-01/" + r["source"])
            self.assertIsNotNone(r["ts"])
        self.assertEqual(STATION_SOURCES[:-1], tuple(s for _, srcs, _ in SENSOR_TABS for s in srcs))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            s = load_settings(path)                                    # missing file
            self.assertEqual((s["host"], s["port"], s["topics"], s["qos"]), ("localhost", 1883, "#", 0))
            self.assertEqual(len(s["graphs"]), 6)
            self.assertIsNone(s["graphs"][0]["topic"])
            self.assertEqual([g["chart"] for g in s["graphs"]], [1, 2, 3, 4, 5, 6])   # each its own
            s["host"], s["tls"] = "broker.lan", True
            s["graphs"][2].update(topic="a/b", field="env.temp", name="T", unit="\N{DEGREE SIGN}C",
                                  scale=0.1, chart=1)
            save_settings(path, s)
            self.assertEqual(load_settings(path), s)
            with open(path, "w") as fh:
                json.dump({"port": True, "tls": 1, "qos": 2, "topics": 5,
                           "graphs": [{"topic": "", "field": 3, "scale": "big", "chart": 9},
                                      {"chart": True}, "junk"]}, fh)
            s = load_settings(path)
            self.assertEqual((s["port"], s["tls"], s["qos"], s["topics"]), (1883, False, 0, "#"))
            self.assertEqual(s["graphs"][0], {"topic": None, "field": "", "name": "", "unit": "",
                                              "scale": 1.0, "chart": 1})
            self.assertEqual(s["graphs"][1]["chart"], 2)
            with open(path, "w") as fh:
                fh.write("not json")
            self.assertEqual(load_settings(path)["host"], "localhost")


class LogImportTests(unittest.TestCase):
    READING = json.dumps({"station": "s1", "source": "co2", "ts": "2026-11-11T10:00:00.250+00:00",
                          "values": {"co2_ppm": 600}})

    def test_reads_back_what_was_logged(self):
        with tempfile.TemporaryDirectory() as d:
            log = MqttCsvLogger(d, "broker.lan")
            t0 = time.mktime((2026, 11, 11, 23, 59, 58, 0, 0, -1))
            log.write(t0, "sim/power/voltage", False, "230.5")
            log.write(t0 + 1, "measure-station/s1/co2", False, self.READING)     # commas and quotes
            log.write(t0 + 3, "sim/status", True, "online")                      # next day's file
            log.close()
            paths = sorted(os.path.join(d, f) for f in os.listdir(d))
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(is_mqtt_log(p) for p in paths))
            with open(paths[0], "a", encoding="utf-8", newline="") as fh:
                fh.write("garbled,row\r\n")
            msgs, skipped = read_mqtt_logs(paths[::-1] + paths[:1])   # out of order, one twice
        self.assertEqual(skipped, 1)
        self.assertEqual([m[1:] for m in msgs], [("sim/power/voltage", False, "230.5"),
                                                 ("measure-station/s1/co2", False, self.READING),
                                                 ("sim/status", True, "online")])
        self.assertEqual([m[0] - t0 for m in msgs], [0, 1, 3])
        self.assertEqual(parse_reading(msgs[1][3])["values"], {"co2_ppm": 600})

    def test_spreadsheet_resave_and_other_files(self):
        with tempfile.TemporaryDirectory() as d:
            resaved = os.path.join(d, "resaved.csv")
            with open(resaved, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh, delimiter=";").writerows([
                    ["pc_time", "topic", "retained", "payload"],
                    ["13/11/2026 09:05", "a/b", "0", '{"x": 1, "y": 2}']])
            other = os.path.join(d, "mqb.csv")
            with open(other, "w") as fh:
                fh.write("pc_time,board_time_est,sensor,gas,t_s,ppm\r\n")
            self.assertTrue(is_mqtt_log(resaved))
            self.assertFalse(is_mqtt_log(other))
            self.assertFalse(is_mqtt_log(os.path.join(d, "missing.csv")))
            msgs, skipped = read_mqtt_logs([resaved])
            with self.assertRaisesRegex(ValueError, "not an MQTT CSV log"):
                read_mqtt_logs([other])
        self.assertEqual((len(msgs), skipped), (1, 0))
        self.assertEqual(time.localtime(msgs[0][0])[:5], (2026, 11, 13, 9, 5))
        self.assertEqual(numeric_fields(msgs[0][3]), {"x": 1.0, "y": 2.0})


if __name__ == "__main__":
    unittest.main()
