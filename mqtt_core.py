"""MQTT subscribing: a minimal MQTT 3.1.1 client (subscribe only), topic
filters, finding the numbers in a payload, the subscriber thread, its CSV
log and the saved broker/graph settings.

Standard library only. QoS 0 and 1 subscriptions; TLS with the system's
certificate store; user name and password if the broker needs them.
"""

import collections
import csv
import json
import math
import os
import random
import socket
import ssl
import struct
import threading
import time
from datetime import datetime

from mqb_core import DailyFile, _day_first, _fmt_time
from mqb_core import parse_time as parse_pc_time

# Packet types (the high nibble of the first byte)
CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 12, 13, 14

CONNACK_CODES = {1: "unacceptable protocol version (the broker needs MQTT 3.1.1)",
                 2: "client identifier rejected", 3: "server unavailable",
                 4: "bad user name or password", 5: "not authorised"}

MAX_PACKET = 16 * 1024 * 1024    # larger is taken as a corrupt stream
MAX_FIELDS = 64                  # numbers taken from one payload
MAX_GRAPHS = 6
KEEPALIVE_S = 30
PING_TIMEOUT_S = 15
RETRY_S = 5.0

Message = collections.namedtuple("Message", "topic payload retain")


class MqttError(Exception):
    """The broker refused the connection (CONNACK return code)."""

    def __init__(self, code):
        self.code = code
        super().__init__("broker refused the connection: %s"
                         % CONNACK_CODES.get(code, "return code %d" % code))


# --- packets --------------------------------------------------------------------

def _varint(n):
    out = bytearray()
    while True:
        n, b = divmod(n, 128)
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def packet(first_byte, body=b""):
    return bytes([first_byte]) + _varint(len(body)) + body


def read_str(body, pos):
    """(string, next position) from a length-prefixed UTF-8 string."""
    if pos + 2 > len(body):
        raise OSError("malformed MQTT packet")
    n = struct.unpack_from(">H", body, pos)[0]
    if pos + 2 + n > len(body):
        raise OSError("malformed MQTT packet")
    return body[pos + 2:pos + 2 + n].decode("utf-8", "replace"), pos + 2 + n


class PacketReader:
    """Splits a byte stream into (first byte, body) packets, however the
    stream happens to be chunked."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data

    def next(self):
        buf = self.buf
        length, mult, i = 0, 1, 1
        while True:
            if i >= len(buf):
                return None
            b = buf[i]
            length += (b & 0x7F) * mult
            i += 1
            if not b & 0x80:
                break
            mult *= 128
            if i > 4:
                raise OSError("malformed MQTT packet length")
        if length > MAX_PACKET:
            raise OSError("MQTT packet too large (%d bytes)" % length)
        if len(buf) < i + length:
            return None
        pkt = buf[0], bytes(buf[i:i + length])
        del buf[:i + length]
        return pkt


def parse_publish(first_byte, body):
    """(Message, qos, packet id or None) from a PUBLISH packet."""
    qos = (first_byte >> 1) & 3
    topic, pos = read_str(body, 0)
    pid = None
    if qos:
        if pos + 2 > len(body):
            raise OSError("malformed MQTT packet")
        pid = struct.unpack_from(">H", body, pos)[0]
        pos += 2
    return Message(topic, body[pos:], bool(first_byte & 1)), qos, pid


# --- topics ---------------------------------------------------------------------

def parse_filters(text):
    """Comma-separated topic filters -> list; ValueError says what is wrong."""
    filters = [f.strip() for f in text.split(",") if f.strip()]
    if not filters:
        raise ValueError("enter at least one topic to subscribe to (# for all)")
    for f in filters:
        levels = f.split("/")
        for i, level in enumerate(levels):
            if "#" in level and (level != "#" or i != len(levels) - 1):
                raise ValueError("%s: # must be the last level, on its own" % f)
            if "+" in level and level != "+":
                raise ValueError("%s: + must be a whole level, e.g. home/+/temp" % f)
    return filters


def topic_matches(flt, topic):
    """Whether a topic name matches a filter (+ one level, # the rest)."""
    f, t = flt.split("/"), topic.split("/")
    if topic.startswith("$") and f[0] in ("+", "#"):
        return False                # $SYS etc. only when asked for by name
    for i, level in enumerate(f):
        if level == "#":
            return True             # "a/#" also matches "a" itself
        if i >= len(t) or (level != "+" and level != t[i]):
            return False
    return len(f) == len(t)


# --- payloads -------------------------------------------------------------------

def payload_text(payload):
    """The payload as text; binary payloads as "hex:" + hex digits."""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return "hex:" + payload.hex()


def numeric_fields(text):
    """Every number in a payload, as {field: value}.

    A plain number (or a JSON number) gives {"": value}. A JSON object or
    list gives one entry per number inside, named by its path, e.g.
    {"env": {"temp": 21.3}, "ok": true} -> {"env.temp": 21.3, "ok": 1.0}.
    Strings that are numbers count; true/false count as 1/0.
    """
    s = text.strip()
    if not s:
        return {}
    try:
        value = json.loads(s)
    except ValueError:
        value = s                   # plain text: a number, or nothing usable
    out = {}
    _flatten(value, "", out)
    return out


def _flatten(v, path, out):
    if len(out) >= MAX_FIELDS:
        return
    if isinstance(v, bool):
        out[path] = float(v)
    elif isinstance(v, (int, float)):
        if math.isfinite(v):
            out[path] = float(v)
    elif isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return
        if math.isfinite(f):
            out[path] = f
    elif isinstance(v, dict):
        for k, x in v.items():
            _flatten(x, "%s.%s" % (path, k) if path else str(k), out)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _flatten(x, "%s.%d" % (path, i) if path else str(i), out)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_time(text):
    """Epoch seconds from an ISO 8601 time with a UTC offset, else None."""
    if not isinstance(text, str):
        return None
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo is not None else None


def parse_reading(text):
    """A Measure Station reading (JSON with station, source and values) as
    {"station", "source", "sensor_type", "ts" (epoch or None), "ts_src",
    "values", "meta"}, or None for any other payload."""
    if not text.lstrip().startswith("{"):
        return None
    try:
        d = json.loads(text)
    except ValueError:
        return None
    if not (isinstance(d, dict) and isinstance(d.get("station"), str)
            and isinstance(d.get("source"), str) and isinstance(d.get("values"), dict)):
        return None
    meta = d.get("meta")
    return {"station": d["station"], "source": d["source"],
            "sensor_type": d.get("sensor_type") if isinstance(d.get("sensor_type"), str) else "",
            "ts": parse_time(d.get("ts")),
            "ts_src": d.get("ts_src") if isinstance(d.get("ts_src"), str) else "",
            "values": d["values"], "meta": meta if isinstance(meta, dict) else {}}


def reading_fields(reading):
    """A reading's numbers as {"values.key" / "meta.key": value}. null is
    kept as None (unknown, e.g. a failed read), so it shows as a gap."""
    return {"%s.%s" % (section, k): None if v is None else float(v)
            for section in ("values", "meta") for k, v in reading[section].items()
            if v is None or (is_number(v) and math.isfinite(v))}


def series_label(topic, field):
    """How a value is named in the window: the topic, plus the JSON field."""
    return topic if not field else "%s : %s" % (topic, field)


# --- client ---------------------------------------------------------------------

class MqttClient:
    """A subscribe-only connection to one broker.

    Network problems and protocol errors raise OSError (socket.timeout and
    ssl errors are OSErrors); a refused connection raises MqttError.
    """

    def __init__(self, host, port=1883, username="", password="", tls=False, client_id=None,
                 keepalive=KEEPALIVE_S, timeout=5.0):
        self.keepalive, self.timeout = keepalive, timeout
        self.reader = PacketReader()
        self.pending = []           # messages that arrived while waiting for an ack
        self.pid = 0
        self.ping_sent = None
        self.sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if tls:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=host)
            self.last_sent = self.last_rx = time.monotonic()
            client_id = client_id or "ddr-%08x" % random.getrandbits(32)
            flags = 0x02                                    # clean session
            body = _str(client_id)
            if username:
                flags |= 0x80
                body += _str(username)
                if password:
                    flags |= 0x40
                    body += _str(password)
            self._send(packet(CONNECT << 4, _str("MQTT") + bytes([4, flags])
                              + struct.pack(">H", keepalive) + body))
            ack = self._wait_for(CONNACK)
            if len(ack) != 2:
                raise OSError("malformed CONNACK")
            if ack[1]:
                raise MqttError(ack[1])
        except BaseException:
            self.sock.close()
            raise

    def subscribe(self, filters, qos=0):
        """Subscribes; returns the broker's code per filter (0x80 = refused)."""
        pid = self._next_pid()
        body = struct.pack(">H", pid) + b"".join(_str(f) + bytes([qos]) for f in filters)
        self._send(packet(SUBSCRIBE << 4 | 2, body))
        ack = self._wait_for(SUBACK)
        if len(ack) != 2 + len(filters) or struct.unpack_from(">H", ack)[0] != pid:
            raise OSError("malformed SUBACK")
        return list(ack[2:])

    def receive(self, timeout):
        """Messages received, returned as soon as there are any or after
        `timeout` seconds. Also keeps the connection alive."""
        msgs, self.pending = self.pending, []
        deadline = time.monotonic() + timeout
        while True:
            self._keepalive()
            pkt = self.reader.next()
            if pkt is not None:
                m = self._handle(*pkt)
                if m is not None:
                    msgs.append(m)
                continue
            left = deadline - time.monotonic()
            if msgs or left <= 0:
                return msgs
            self._fill(left)

    def close(self):
        try:
            self.sock.settimeout(1)
            self.sock.sendall(packet(DISCONNECT << 4))
        except OSError:
            pass
        self.sock.close()

    # internals ----------------------------------------------------------------
    def _next_pid(self):
        self.pid = self.pid % 0xFFFF + 1
        return self.pid

    def _send(self, data):
        self.sock.settimeout(self.timeout)
        self.sock.sendall(data)
        self.last_sent = time.monotonic()

    def _fill(self, timeout):
        self.sock.settimeout(max(0.01, min(timeout, 1.0)))
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return
        if not data:
            raise OSError("connection closed by the broker")
        self.last_rx = time.monotonic()
        self.reader.feed(data)

    def _wait_for(self, kind):
        deadline = time.monotonic() + self.timeout
        while True:
            pkt = self.reader.next()
            if pkt is not None:
                if pkt[0] >> 4 == kind:
                    return pkt[1]
                m = self._handle(*pkt)
                if m is not None:
                    self.pending.append(m)
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("no reply from the broker")
            self._fill(left)

    def _handle(self, first_byte, body):
        kind = first_byte >> 4
        if kind == PUBLISH:
            m, qos, pid = parse_publish(first_byte, body)
            if qos == 1:
                self._send(packet(PUBACK << 4, struct.pack(">H", pid)))
            elif qos == 2:
                self._send(packet(PUBREC << 4, struct.pack(">H", pid)))
            return m
        if kind == PUBREL and len(body) >= 2:
            self._send(packet(PUBCOMP << 4, body[:2]))
        elif kind == PINGRESP:
            self.ping_sent = None
        return None

    def _keepalive(self):
        now = time.monotonic()
        if self.ping_sent is not None:
            # Anything received since the ping shows the link is up.
            if self.last_rx < self.ping_sent and now - self.ping_sent > PING_TIMEOUT_S:
                raise OSError("broker stopped answering")
            if self.last_rx >= self.ping_sent:
                self.ping_sent = None
        elif now - self.last_sent >= self.keepalive * 0.75:
            self._send(packet(PINGREQ << 4))
            self.ping_sent = time.monotonic()


# --- logging --------------------------------------------------------------------

class MqttCsvLogger:
    """One row per message with its raw payload, so any value in it can be
    graphed later, whatever the graph setup was at the time."""

    HEADER = "pc_time,topic,retained,payload\r\n"

    def __init__(self, folder, host):
        name = "mqtt_" + "".join(c if c.isalnum() or c in ".-" else "_" for c in host)
        self.daily = DailyFile(folder, name, ".csv", self.HEADER, encoding="utf-8")

    @property
    def path(self):
        return self.daily.path

    def write(self, now, topic, retained, text):
        fh = self.daily.file_for(now)
        csv.writer(fh).writerow([_fmt_time(now), topic, 1 if retained else 0, text])
        fh.flush()

    def close(self):
        self.daily.close()


def _log_delimiter(header):
    return ";" if header.count(";") > header.count(",") else ","


def is_mqtt_log(path):
    """Whether a file starts with an MQTT CSV log's header (topic and payload columns)."""
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            header = fh.readline()
    except OSError:
        return False
    cols = {c.strip() for c in header.strip().split(_log_delimiter(header))}
    return {"pc_time", "topic", "payload"} <= cols


def read_mqtt_logs(paths):
    """Messages back from CSV logs written by MqttCsvLogger, to replay onto the charts.

    Returns (messages, skipped): messages is [(pc_time, topic, retained, text)]
    in time order from all the files, each row once even if a file is given
    twice; skipped counts rows that could not be read. Also reads a log a
    spreadsheet has re-saved (other date styles, ';' separators, a byte order
    mark). Raises ValueError for a file that is not an MQTT CSV log.
    """
    csv.field_size_limit(max(csv.field_size_limit(), MAX_PACKET))   # payloads can be long
    seen, skipped = {}, 0       # a dict keeps file order for rows logged in the same second
    for path in dict.fromkeys(os.path.normcase(os.path.abspath(p)) for p in paths):
        name = os.path.basename(path)
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            header = fh.readline()
            fh.seek(0)
            reader = csv.DictReader(fh, delimiter=_log_delimiter(header))
            reader.fieldnames = [f.strip() for f in reader.fieldnames or ()]
            missing = {"pc_time", "topic", "payload"} - set(reader.fieldnames)
            if missing:
                raise ValueError("%s is not an MQTT CSV log (no %s column)"
                                 % (name, ", ".join(sorted(missing))))
            try:
                rows = list(reader)
            except csv.Error as e:
                raise ValueError("%s: %s" % (name, e)) from None
        day_first = _day_first(r["pc_time"] for r in rows)
        for r in rows:
            try:
                t = parse_pc_time(r["pc_time"], day_first)
                topic, text = r["topic"].strip(), r["payload"]
                if not topic or text is None:
                    raise ValueError("short row")
            except (AttributeError, TypeError, ValueError, OverflowError):
                skipped += 1
                continue
            seen[(t, topic, (r.get("retained") or "").strip() == "1", text)] = None
    return sorted(seen, key=lambda m: m[0]), skipped


# --- subscriber -----------------------------------------------------------------

class MqttSubscriber:
    """Stays subscribed to a broker on a background thread, reconnecting
    whenever the connection is lost.

    emit(kind, payload) is called on the worker thread with:
      ("message", (now, topic, text, fields, retained, reading))
                                 text: payload_text(); reading: parse_reading() (a
                                 Measure Station reading, else None); fields:
                                 reading_fields() for a reading, else numeric_fields()
      ("log", (level, text))     connection changes and errors, each once
    """

    def __init__(self, host, port, topics, qos, emit, log_folder, username="", password="",
                 tls=False, retry=RETRY_S):
        self.host, self.port, self.topics, self.qos = host, port, topics, qos
        self.username, self.password, self.tls = username, password, tls
        self.retry = retry
        self.emit = emit
        self.csv = MqttCsvLogger(log_folder, host)
        self.messages = self.failed = 0
        self.connected = False
        self.last_rx = None
        self._error = None          # last error reported, so each is logged once
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def where(self):
        return "%s:%d" % (self.host, self.port)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _log(self, level, text):
        self.emit("log", (level, text))

    def _run(self):
        client = None
        self._log("info", "connecting to %s%s, subscribing to %s (QoS %d)" % (
            self.where, " with TLS" if self.tls else "", ", ".join(self.topics), self.qos))
        try:
            while not self.stop_event.is_set():
                try:
                    if client is None:
                        client = MqttClient(self.host, self.port, self.username, self.password,
                                            self.tls)
                        codes = client.subscribe(self.topics, self.qos)
                        self.connected = True
                        self._error = None
                        self._log("info", "connected to %s" % self.where)
                        for t, code in zip(self.topics, codes):
                            if code == 0x80:
                                self._log("warning", "broker refused the subscription to %s" % t)
                    for m in client.receive(0.5):
                        self._message(m)
                except (MqttError, OSError) as e:
                    if client is not None:
                        client.close()
                        client = None
                    what = ("lost the connection to %s" if self.connected
                            else "cannot connect to %s") % self.where
                    self.connected = False
                    self.failed += 1
                    self._fail("error", "%s: %s (retrying)" % (what, str(e) or type(e).__name__))
                    self.stop_event.wait(self.retry)
        except Exception as e:                      # keep the GUI alive
            self._log("error", "MQTT subscriber stopped: %r" % e)
        finally:
            if client is not None:
                client.close()
            self.connected = False
            self.csv.close()

    def _fail(self, level, text):
        if text != self._error:
            self._log(level, text)
            self._error = text

    def _message(self, m):
        now = time.time()
        text = payload_text(m.payload)
        self.csv.write(now, m.topic, m.retain, text)
        self.messages += 1
        self.last_rx = now
        reading = parse_reading(text)
        fields = reading_fields(reading) if reading else numeric_fields(text)
        self.emit("message", (now, m.topic, text, fields, m.retain, reading))


# --- settings -------------------------------------------------------------------

UNITS = ("", "\N{DEGREE SIGN}C", "%RH", "hPa", "ppm", "ppb", "V", "A", "W", "kWh", "%")

DEFAULT_SETTINGS = {
    "host": "localhost", "port": 1883, "tls": False, "username": "", "password": "",
    "topics": "#", "qos": 0, "sim": False, "station": "", "graphs": [],
}

# A Custom page value; "chart" is the graph it is drawn on (1-6, shared by
# values with the same number), by default its own.
EMPTY_GRAPH = {"topic": None, "field": "", "name": "", "unit": "", "scale": 1.0, "chart": None}


def load_settings(path):
    """Saved settings merged over the defaults; bad or missing values fall back."""
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        for k, default in DEFAULT_SETTINGS.items():
            v = saved.get(k)
            if k != "graphs" and type(v) is type(default):
                s[k] = v
        if isinstance(saved.get("graphs"), list):
            s["graphs"] = [g for g in saved["graphs"] if isinstance(g, dict)]
    if s["qos"] not in (0, 1):
        s["qos"] = 0
    graphs = []
    for i, g in enumerate((s["graphs"] + [{}] * MAX_GRAPHS)[:MAX_GRAPHS]):
        g = dict(EMPTY_GRAPH, **g)
        if isinstance(g["chart"], bool) or g["chart"] not in range(1, MAX_GRAPHS + 1):
            g["chart"] = i + 1
        if not isinstance(g["topic"], str) or not g["topic"]:
            g["topic"] = None
        for k in ("field", "name", "unit"):
            if not isinstance(g[k], str):
                g[k] = ""
        if isinstance(g["scale"], bool) or not isinstance(g["scale"], (int, float)) \
                or not math.isfinite(g["scale"]):
            g["scale"] = 1.0
        graphs.append(g)
    s["graphs"] = graphs
    return s


def save_settings(path, settings):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, path)
