"""What a Measure Station publishes and how the MQTT tab shows it: the sensor
tabs, value names and units, which values get large graphs, and each
reading's status from the quality rules in the station's Server Pi Interface.

Anything not listed here still works: an unknown source gets a tab of its
own, and an unknown key gets a graph (if it is a number) or a place in the
details (if not), named from its <quantity>_<unit> key.
"""

import statistics

from mqtt_core import is_number

MQ_SOURCES = ("mqboard-mq2", "mqboard-mq4", "mqboard-mq5", "mqboard-mq7", "mqboard-mq8",
              "mqboard-mq135")

# (tab title, sources shown on it, seconds between a source's readings)
SENSOR_TABS = [
    ("Temperature", ("temperature",), 60),
    ("Humidity", ("humidity",), 60),
    ("Barometer", ("barometer",), 60),
    ("CO2", ("co2",), 60),
    ("Particulates", ("particulates",), 60),
    ("Light", ("light",), 60),
    ("Air quality", ("air-quality",), 60),
    ("MQ board", MQ_SOURCES, 150),
]
HEALTH_TAB = ("System health", ("health",), 60)
DEFAULT_INTERVAL = 60
KNOWN_SOURCES = {s for _, sources, _ in SENSOR_TABS + [HEALTH_TAB] for s in sources}

# Keys every source on a page reports identically, so they get one graph
# (from the first source sending it), with this title. The MQ board's six
# sensors are all compensated with the board's one temperature sensor.
SHARED = {MQ_SOURCES: {("meta", "temperature_c"): "Board temp"}}


def shared_keys(sources):
    """{(section, key): title} for a page showing these sources."""
    return SHARED.get(tuple(sources), {})


# health has no meta, so these of its values get the large graphs, in this
# order; the rest get small ones.
HEALTH_LARGE = ("cpu_usage_pct", "cpu_temperature_c", "ram_usage_pct", "outbox_backlog",
                "fan_temp_diff_c", "fan_exhaust_duty_pct")

LABELS = {
    "temperature_c": "Temperature", "humidity_pct": "Humidity", "pressure_hpa": "Pressure",
    "co2_ppm": "CO2", "pm1_ugm3": "PM1", "pm25_ugm3": "PM2.5", "pm4_ugm3": "PM4",
    "pm10_ugm3": "PM10", "voc_index": "VOC index", "nox_index": "NOx index",
    "illuminance_lux": "Illuminance", "aqi": "Air quality index (1-5)", "tvoc_ppb": "TVOC",
    "eco2_ppm": "eCO2 (estimated, not CO2)", "concentration_ppm": "Concentration",
    "r0_kohm": "R0",
    "uptime_s": "Service uptime", "outbox_backlog": "Outbox backlog", "queue_depth": "Queue depth",
    "task_restarts": "Task restarts", "awaiting_time_sync": "Awaiting time sync",
    "cpu_usage_pct": "CPU usage", "cpu_temperature_c": "CPU temperature", "load_1m": "Load (1 min)",
    "disk_free_mb": "Disk free", "mem_available_mb": "Memory available", "ram_usage_pct": "RAM usage",
    "mqboard_last_line_age_s": "MQ board last line", "mqboard_bad_lines": "MQ board bad lines",
    "mqboard_held": "MQ board held", "fan_exhaust_duty_pct": "Exhaust fans",
    "fan_intake_duty_pct": "Intake fans", "fan_temp_diff_c": "Enclosure temp rise (probe - humidity)",
}

SUFFIX_UNITS = (("_ugm3", "\N{MICRO SIGN}g/m\N{SUPERSCRIPT THREE}"), ("_kohm", "k\N{OHM SIGN}"),
                ("_lux", "lx"), ("_pct", "%"), ("_hpa", "hPa"), ("_ppm", "ppm"), ("_ppb", "ppb"),
                ("_mb", "MB"), ("_c", "\N{DEGREE SIGN}C"), ("_s", "s"))

# Shown in other units than sent: key -> (multiplier, unit)
DISPLAY = {"uptime_s": (1 / 3600, "h")}


def line_gap(times, floor=30.0):
    """Seconds without a reading that break a chart's line: 3x the usual
    interval between the latest (up to 50) times, so sources publishing every
    10 s, 60 s or 150 s each stay joined, but never less than `floor`."""
    tail = times[-51:]
    steps = [b - a for a, b in zip(tail, tail[1:]) if b > a]
    return max(floor, 3 * statistics.median(steps)) if steps else floor


def ago(seconds):
    s = int(max(0, seconds))
    return "%ds" % s if s < 120 else "%dm" % (s // 60) if s < 7200 else \
        "%dh" % (s // 3600) if s < 172800 else "%dd" % (s // 86400)


def unit(key):
    if key in DISPLAY:
        return DISPLAY[key][1]
    if key.startswith("humidity"):
        return "%RH"
    return next((u for suffix, u in SUFFIX_UNITS if key.endswith(suffix)), "")


def scale(key):
    return DISPLAY[key][0] if key in DISPLAY else 1.0


def label(key):
    if key in LABELS:
        return LABELS[key]
    stem = next((key[:-len(s)] for s, _ in SUFFIX_UNITS if key.endswith(s)), key)
    stem = stem.replace("_", " ").strip() or key
    return stem[0].upper() + stem[1:]


def is_large(source, section, key):
    if source == "health":
        return key in HEALTH_LARGE
    return section == "values"


def source_title(source, reading=None):
    """How a source is named on a tab showing several, e.g. "MQ4 CH4"."""
    if source.startswith("mqboard-"):
        name = source[len("mqboard-"):].upper()
        gas = reading["meta"].get("gas") if reading else None
        return "%s %s" % (name, gas) if isinstance(gas, str) else name
    return source


def _fmt(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if v is None:
        return "unknown"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "none"
    return str(v)


def details(reading, numeric):
    """The reading's keys that aren't graphed: text codes, flags, booleans,
    lists. `numeric`: {"values": [...], "meta": [...]} keys that are graphed
    (their nulls are shown on the graph, not here)."""
    parts = []
    for section in ("values", "meta"):
        for k, v in reading[section].items():
            if is_number(v) or (v is None and k in numeric.get(section, ())):
                continue
            parts.append("%s: %s" % (k, _fmt(v)))
    return "   ".join(parts)


def assess(source, reading, age, interval, station_status=None):
    """(level, text) for a source's latest reading: level "ok", "warn" or
    "bad"; text lists every problem, or says "ok"."""
    issues = []
    v, m = reading["values"], reading["meta"]

    def add(level, text):
        issues.append((level, text))

    if age is not None and age > 2.5 * interval:
        add("warn", "no reading for %s" % ago(age))
    if reading.get("ts_src") == "estimated":
        add("warn", "time estimated (station clock not synced)")

    if source == "air-quality":
        validity = m.get("validity")
        if isinstance(validity, str) and validity != "normal":
            add("bad" if validity == "invalid" else "warn",
                "validity %s: don't trust the values yet" % validity)
    elif source == "particulates":
        if m.get("pm"):
            add("bad", "PM may be wrong (%s)" % m["pm"])
        if m.get("voc"):
            add("warn", "VOC index %s (under 1 h since reset)" % m["voc"])
        if m.get("nox"):
            add("warn", "NOx index %s (under 6 h since reset)" % m["nox"])
        if any(x is None for x in v.values()):
            add("warn", "some values unknown")
        if is_number(v.get("humidity_pct")) and v["humidity_pct"] > 80:
            add("warn", "humid: PM reads high above 80 %RH")
    elif source == "co2":
        if m.get("comp_src") == "alt":
            add("warn", "compensated by altitude, not live pressure")
    elif source.startswith("mqboard-"):
        if "concentration_ppm" in v and v["concentration_ppm"] is None:
            add("bad", "read failed (no ppm)")
        if m.get("baseline_tracking") is False:
            if m.get("r0_kohm") == 10.0:
                add("bad", "boot calibration failed (placeholder R0)")
            else:
                add("warn", "provisional baseline: shows changes, not reliable levels")
        if m.get("ts_basis") == "receipt":
            add("warn", "timestamped on receipt (up to 150 s late)")
    elif source == "health":
        if station_status == "offline":
            add("bad", "station offline")
        if v.get("clock_synced") is False:
            add("bad", "clock not synchronised")
        if is_number(v.get("awaiting_time_sync")) and v["awaiting_time_sync"] > 0:
            add("warn", "%d readings awaiting time sync" % v["awaiting_time_sync"])
        if v.get("mqboard_up") is False:
            add("bad", "MQ board link down")
        if v.get("mqboard_stale_sensors"):
            add("warn", "MQ board sensors stale: %s" % _fmt(v["mqboard_stale_sensors"]))
        if v.get("fan_failsafe") is True:
            add("warn", "fan failsafe: a reading fan control needs is missing")
        if is_number(v.get("fan_intake_duty_pct")) and v["fan_intake_duty_pct"] > 0:
            add("warn", "intake fans on: sensor readings may be slightly warm")
        if v.get("fan_fog") is True:
            add("ok", "fog mode")
        if is_number(v.get("cpu_temperature_c")) and v["cpu_temperature_c"] > 80:
            add("bad", "CPU over 80 \N{DEGREE SIGN}C (throttling)")
        if is_number(v.get("outbox_backlog")) and v["outbox_backlog"] > 100:
            add("warn", "outbox backlog %d" % v["outbox_backlog"])

    if not issues:
        return "ok", "ok"
    levels = {lv for lv, _ in issues}
    level = "bad" if "bad" in levels else "warn" if "warn" in levels else "ok"
    return level, "; ".join(t for _, t in issues)
