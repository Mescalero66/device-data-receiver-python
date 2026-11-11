"""The MQTT tab: broker settings, then pages of its own: one per Measure
Station sensor, one for the station's health, and Custom (up to six values
picked from any topic, on graphs of your choosing, several to a graph).

A MqttSubscriber thread reports through the tab's queue. Every number in a
message is kept as a series named by topic and field, e.g. ("measure-station/
mstn-2b/co2", "values.co2_ppm"). Station readings are plotted at their
capture time `ts`, so a backlog delivered late lands where it belongs, and a
reading delivered twice (QoS 1) is kept once.
"""

import bisect
import os
import queue
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from gui_common import (CATEGORICAL, CRITICAL, DASHES, INK_2, MUTED, PAGE, SERIES, SURFACE, WINDOWS,
                        TabBase)
from mqtt_core import (MAX_GRAPHS, UNITS, MqttSubscriber, is_number, load_settings, numeric_fields,
                       parse_filters, parse_reading, read_mqtt_logs, reading_fields, save_settings,
                       series_label, topic_matches)
from station_spec import DEFAULT_INTERVAL, HEALTH_TAB, KNOWN_SOURCES, SENSOR_TABS, ago, line_gap
from station_view import SensorView

MAX_POINTS = 20000              # values kept per series for plotting (~2 weeks at 1/min)
MAX_TOPICS = 1000               # topics tracked and charted; any more are only logged
NONE = "(none)"


class MqttTab(TabBase):
    def __init__(self, root, frame, notebook, out, settings_path, connect=False, sim=False, csv=()):
        super().__init__(root, frame, notebook)
        self.out = out
        self.settings_path = settings_path
        self.s = load_settings(settings_path)
        # --mqtt-sim is for this run only; the saved choice stays as it was.
        self.saved_sim = None if not sim else self.s["sim"]
        if sim:
            self.s["sim"] = True
        self.sub = None
        self.broker = None
        self.gen = 0                # connection number; stale worker messages are dropped
        self.source = None          # identifies the broker the data came from
        self.series = {}            # (topic, field) -> [(time, value or None, note)], in time order
        self.numeric = set()        # series with at least one number (offered for Custom graphs)
        self.readings = {}          # (station, source) -> {"topic", "reading", "t", "count"}
        self.keys = {}              # (station, source) -> {"values": [...], "meta": [...]} numeric keys
        self.station_status = {}    # station -> "online" / "offline" (its retained status topic)
        self.new_series = False
        self.views = []             # sensor pages, health last
        self.loading = True
        self._build()
        self.loading = False
        self.custom.graphs_changed(save=False)
        if csv:
            self._import(csv)
        if connect or sim:
            self._connect()
        root.after(200, self._poll)
        root.after(1000, self._tick)

    # layout -----------------------------------------------------------
    def _build(self):
        f, s = self.frame, self.s
        self.v = {k: tk.StringVar(value=str(s[k])) for k in
                  ("host", "port", "username", "password", "topics", "qos")}
        self.tls_var = tk.BooleanVar(value=s["tls"])
        self.sim_var = tk.BooleanVar(value=s["sim"])
        self.inputs = []            # widgets locked while connected

        def entry(parent, label, key, width, padx=10, **kw):
            ttk.Label(parent, text=label).pack(side="left", padx=(padx, 4))
            e = ttk.Entry(parent, textvariable=self.v[key], width=width, **kw)
            e.pack(side="left")
            self.inputs.append(e)
            return e

        bar = ttk.Frame(f, padding=(8, 8, 8, 2))
        bar.pack(fill="x")
        self.broker_inputs = [entry(bar, "Broker", "host", 24, 0), entry(bar, "Port", "port", 6)]
        tls = ttk.Checkbutton(bar, text="TLS", variable=self.tls_var, command=self._tls_toggled)
        tls.pack(side="left", padx=(10, 0))
        self.broker_inputs += [tls, entry(bar, "User", "username", 14, 16),
                               entry(bar, "Password", "password", 14, show="*")]
        self.inputs.append(tls)
        sim_box = ttk.Checkbutton(bar, text="Simulated broker", variable=self.sim_var,
                                  command=self._sim_toggled)
        sim_box.pack(side="left", padx=(16, 0))
        self.inputs.append(sim_box)
        self.conn_btn = ttk.Button(bar, text="Connect", command=self._toggle)
        self.conn_btn.pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="Open log folder", command=self._open_folder).pack(side="right")

        bar2 = ttk.Frame(f, padding=(8, 2, 8, 4))
        bar2.pack(fill="x")
        entry(bar2, "Subscribe to", "topics", 44, 0)
        ttk.Label(bar2, text="QoS").pack(side="left", padx=(10, 4))
        qos = ttk.Combobox(bar2, textvariable=self.v["qos"], values=("0", "1"), state="readonly", width=3)
        qos.pack(side="left")
        self.inputs.append(qos)
        ttk.Label(bar2, text="comma-separated; + matches one level, # everything below",
                  style="Status.TLabel").pack(side="left", padx=10)
        self.file_var = tk.StringVar(value="Not connected")
        ttk.Label(bar2, textvariable=self.file_var, style="Status.TLabel").pack(side="right")
        self._sim_toggled()

        self.status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.status_var, style="Status.TLabel",
                  padding=(8, 0, 8, 4)).pack(fill="x")

        # Chart controls shared by every page.
        ctl = ttk.Frame(f, padding=(8, 0, 8, 4))
        ctl.pack(fill="x")
        ttk.Label(ctl, text="Station").pack(side="left")
        self.station_var = tk.StringVar(value=s["station"])
        self.station_box = ttk.Combobox(ctl, textvariable=self.station_var, state="readonly", width=16,
                                        values=[s["station"]] if s["station"] else [])
        self.station_box.pack(side="left", padx=(6, 16))
        self.station_box.bind("<<ComboboxSelected>>", lambda e: self._station_chosen())
        ttk.Label(ctl, text="Show").pack(side="left")
        self.window_var = tk.StringVar(value="6 hours")
        box = ttk.Combobox(ctl, textvariable=self.window_var, state="readonly", width=10,
                           values=[w[0] for w in WINDOWS])
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        self.fixed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Fix axis", variable=self.fixed_var,
                        command=self._mark_dirty).pack(side="left", padx=(8, 0))
        ttk.Button(ctl, text="Import CSV...", command=self._import).pack(side="left", padx=(12, 4))
        ttk.Button(ctl, text="Clear charts", command=self._clear_charts).pack(side="left")
        self._events_checkbox(ctl).pack(side="left", padx=12)
        ttk.Label(ctl, text="Hover a chart for values. Station readings are plotted at the time they "
                            "were taken. Hollow points: estimated time; red crosses: unknown (null).",
                  style="Status.TLabel").pack(side="right")

        panes = ttk.PanedWindow(f, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.pages = ttk.Notebook(panes)
        panes.add(self.pages, weight=4)
        for title, sources, interval in SENSOR_TABS + [HEALTH_TAB]:
            view = SensorView(self, self.pages, title, sources, interval)
            self.pages.add(view.frame, text=" %s " % title)
            self.views.append(view)
        self.health = self.views[-1]
        self.custom = CustomView(self, self.pages)
        self.pages.add(self.custom.frame, text=" Custom ")
        self.pages.bind("<<NotebookTabChanged>>", lambda e: self._mark_dirty())
        self._build_events(panes)

    def _add_view(self, source):
        """A page for a source not in the station's documents, before health."""
        view = SensorView(self, self.pages, source, (source,), DEFAULT_INTERVAL)
        self.pages.insert(self.health.frame, view.frame, text=" %s " % source)
        self.views.insert(len(self.views) - 1, view)

    def _current_page(self):
        sel = self.pages.select()
        return next((v for v in self.views + [self.custom] if str(v.frame) == sel), None)

    def _mark_dirty(self):
        self.plot_dirty = True
        for v in getattr(self, "views", []):
            v.dirty = True
        if getattr(self, "custom", None):
            self.custom.dirty = True

    # helpers for the pages ----------------------------------------------------
    def station(self):
        return self.station_var.get() or None

    def span(self):
        return dict(WINDOWS)[self.window_var.get()]

    def points(self, station, source, section, key):
        info = self.readings.get((station, source))
        return self.series.get((info["topic"], "%s.%s" % (section, key)), []) if info else []

    def view_end(self, series, now):
        """Charts end at the present while connected, else at the newest data."""
        if self.sub:
            return now
        last = [p[-1][0] for p in series if p]
        return max(last) if last else now

    def plot(self, ax, pts, unit, xlim, fontsize, mult=1.0, color=SERIES, ls="-", alone=True):
        """pts: [(time, value or None, note)]. None (unknown) is a gap with a
        red cross; a point with a note (estimated time) is hollow; a point
        with no neighbour to join gets a dot. fontsize None: no newest-value
        label (small graphs put it in their title). alone=False: one of
        several lines on a chart, so the caller sets its y limits and hover.
        Returns the values plotted (after `mult`)."""
        pts = [(t, None if v is None else v * mult, n) for t, v, n in pts]
        gap = line_gap([p[0] for p in pts])
        self._line(ax, [(t, v) for t, v, n in pts], gap, color, ls)

        def joined(i, j):
            return 0 <= j < len(pts) and pts[j][1] is not None and abs(pts[j][0] - pts[i][0]) <= gap

        lone = [(datetime.fromtimestamp(t), v) for i, (t, v, n) in enumerate(pts)
                if v is not None and not joined(i, i - 1) and not joined(i, i + 1)]
        if lone:
            ax.plot(*zip(*lone), lw=0, marker="o", markersize=3.5, color=color, zorder=4)
        noted = [(datetime.fromtimestamp(t), v) for t, v, n in pts if n and v is not None]
        if noted:
            ax.plot(*zip(*noted), lw=0, marker="o", markersize=5, color=color,
                    markerfacecolor=SURFACE, markeredgewidth=1.2, zorder=4)
        nulls = [datetime.fromtimestamp(t) for t, v, n in pts if v is None]
        if nulls:
            ax.plot(nulls, [0.04] * len(nulls), transform=ax.get_xaxis_transform(), lw=0, marker="x",
                    markersize=6, markeredgewidth=1.5, color=CRITICAL, zorder=4)
        valid = [(t, v) for t, v, n in pts if v is not None]
        self._latest(ax, valid, fontsize if alone else None, color)
        ax.set_xlim(*xlim)
        if alone:
            if self.fixed_var.get() and valid:
                self._zero_based(ax, [v for t, v in valid])
            self._set_hover(ax, unit, pts, "unknown (null)")
        return pts

    # settings -----------------------------------------------------------
    def _read_connection(self):
        """Connection settings from the fields; ValueError says what is wrong."""
        try:
            port = int(self.v["port"].get().strip())
        except ValueError:
            raise ValueError("Port must be a number") from None
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        c = {"host": self.v["host"].get().strip(), "port": port, "tls": self.tls_var.get(),
             "username": self.v["username"].get().strip(), "password": self.v["password"].get(),
             "topics": ", ".join(parse_filters(self.v["topics"].get())),
             "qos": int(self.v["qos"].get()), "sim": self.sim_var.get()}
        if not c["sim"]:
            if not c["host"]:
                raise ValueError("enter the broker's address")
            if c["password"] and not c["username"]:
                raise ValueError("MQTT needs a user name to send a password")
        return c

    def _save(self):
        data = dict(self.s)
        if self.saved_sim is not None and data["sim"]:
            data["sim"] = self.saved_sim
        try:
            save_settings(self.settings_path, data)
        except OSError as e:
            self._add_log("warning", "could not save MQTT settings: %s" % e)

    def _sim_toggled(self):
        state = "disabled" if self.sim_var.get() or self.sub else "normal"
        for w in self.broker_inputs:
            w.configure(state=state)

    def _tls_toggled(self):
        # Swap between the standard ports if the other one is set.
        port = self.v["port"].get().strip()
        if self.tls_var.get() and port == "1883":
            self.v["port"].set("8883")
        elif not self.tls_var.get() and port == "8883":
            self.v["port"].set("1883")

    def _station_chosen(self):
        self.s["station"] = self.station_var.get()
        self._save()
        self._mark_dirty()

    def _station_seen(self, station):
        values = list(self.station_box["values"])
        if station not in values:
            self.station_box["values"] = sorted(values + [station])
        if not self.station_var.get():
            self.station_var.set(station)
            self._station_chosen()

    # connection -------------------------------------------------------------
    def _toggle(self):
        if self.sub:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        try:
            c = self._read_connection()
        except ValueError as e:
            messagebox.showerror("MQTT", str(e), parent=self.root)
            return
        self.s.update(c)
        self.v["topics"].set(c["topics"])
        self._save()
        host, port, tls, user, pw = c["host"], c["port"], c["tls"], c["username"], c["password"]
        topics = parse_filters(c["topics"])
        if c["sim"]:
            from mqtt_sim import TOPICS, SimBroker
            if self.broker is None:
                self.broker = SimBroker().start()
            host, port, tls, user, pw = "127.0.0.1", self.broker.port, False, "", ""
            if not any(topic_matches(f, t) for f in topics for t in TOPICS):
                self._add_log("warning", "the simulated broker publishes sim/... and measure-station/"
                                         "sim-01/...; none of them match the topics subscribed to")
        source = "sim" if c["sim"] else "%s:%d" % (host, port)
        # Not after only imported logs (no source yet): live data joins them.
        if self.series and self.source not in (None, source):
            self._clear_all()
            self._add_log("info", "topics and charts cleared: different broker")
        self.source = source
        folder = os.path.join(self.out, "sim") if c["sim"] else self.out
        self.gen += 1
        emit = lambda kind, payload, gen=self.gen: self.q.put((gen, kind, payload))
        self.sub = MqttSubscriber(host, port, topics, c["qos"], emit, folder, user, pw, tls)
        self.sub.start()
        for w in self.inputs:
            w.configure(state="disabled")
        self.conn_btn.configure(text="Disconnect")
        self.file_var.set("Logging to " + os.path.abspath(folder))
        self._mark_dirty()

    def _disconnect(self):
        # Don't wait: the subscriber may be in a connect timeout. Its late
        # messages carry an old generation number and are ignored.
        self.sub.stop_event.set()
        self.sub = None
        self.gen += 1
        self._add_log("info", "disconnected")
        for w in self.inputs:
            w.configure(state="readonly" if isinstance(w, ttk.Combobox) else "normal")
        self._sim_toggled()
        self.conn_btn.configure(text="Connect")
        self.file_var.set("Not connected")

    def _log_folder(self):
        return os.path.join(self.out, "sim") if self.sim_var.get() else self.out

    def _open_folder(self):
        folder = self._log_folder()
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    # imported logs ----------------------------------------------------------
    def _import(self, paths=None):
        """Replays logged messages onto every page, as if they had just arrived."""
        if paths is None:
            folder = self._log_folder()
            paths = filedialog.askopenfilenames(
                parent=self.root, title="Import MQTT CSV logs",
                initialdir=os.path.abspath(folder if os.path.isdir(folder) else self.out),
                filetypes=[("MQTT logs", "mqtt_*.csv"), ("CSV files", "*.csv"), ("All files", "*.*")])
            if not paths:
                return
        try:
            messages, skipped = read_mqtt_logs(paths)
        except (OSError, ValueError) as e:
            messagebox.showerror("Import CSV", str(e), parent=self.root)
            return
        added = already = 0
        times = []
        for rx, topic, retained, text in messages:
            reading = parse_reading(text)
            fields = reading_fields(reading) if reading else numeric_fields(text)
            t = self._message(rx, topic, text, fields, retained, reading, live=False)
            if t is None:
                already += 1 if fields else 0
            else:
                added += 1
                times.append(t)

        names = ", ".join(os.path.basename(p) for p in paths)
        if not messages:
            self._add_log("warning", "no messages found in %s" % names)
            return
        text = "imported %d messages from %s (%s to %s)" % (
            len(messages), names, *(datetime.fromtimestamp(m[0]).strftime("%d %b %H:%M")
                                    for m in (messages[0], messages[-1])))
        if already:
            text += ", %d already shown" % already
        if skipped:
            text += ", %d unreadable row(s) skipped" % skipped
        self._add_log("info", text)

        # Widen the chart window if the imported data would not fit.
        if times:
            need = self.view_end(self.series.values(), time.time()) - min(times)
            if self.span() is not None and self.span() < need:
                self.window_var.set(next(w for w, s in WINDOWS if s is None or s >= need))
        self._mark_dirty()

    def shutdown(self):
        if self.sub:
            self.sub.stop_event.set()
        if self.broker:
            self.broker.stop()

    # data flow ----------------------------------------------------------------
    def _poll(self):
        try:
            for _ in range(2000):
                gen, kind, payload = self.q.get_nowait()
                if gen != self.gen:
                    continue
                if kind == "message":
                    self._message(*payload)
                elif kind == "log":
                    self._add_log(*payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _message(self, now, topic, text, fields, retained, reading, live=True):
        """Stores a message's values at the time they belong to, which it
        returns; None if nothing was stored (no numbers, too many topics, or
        a copy of values already kept). live=False: replayed from a log."""
        if not self.custom.topic_message(now, topic, text, retained):
            return None                                 # too many topics
        # <base>/<station>/status. Only readings add a station to the list,
        # so another device's "x/status" doesn't become one. A logged status
        # is history: it only stands in while there is no live one.
        parts = topic.split("/")
        if (len(parts) >= 3 and parts[-1] == "status" and text.strip() in ("online", "offline")
                and (live or not self.sub)):
            self.station_status[parts[-2]] = text.strip()
            self.health.dirty = True
        t, note = now, None
        if reading:
            if reading["ts"] is not None:
                t = reading["ts"]
            if reading["ts_src"] == "estimated":
                note = "time estimated (clock not synced)"
            if self._duplicate(topic, fields, t):
                return None                             # QoS 1 resend, or imported and already kept
            self._reading(topic, t, reading)
        elif not live and self._duplicate(topic, fields, t, slack=1.0):
            # Logged times are whole seconds; the live copy had the fraction.
            return None
        for field, v in fields.items():
            self._insert((topic, field), t, v, note)
        return t if fields else None

    def _duplicate(self, topic, fields, t, slack=0.0):
        """Whether these values are already kept: a point at exactly t (a
        reading's capture time), or with slack, the same value within slack s."""
        for field, v in fields.items():
            pts = self.series.get((topic, field))
            if pts:
                i = bisect.bisect_left(pts, t - slack, key=lambda p: p[0])
                while i < len(pts) and pts[i][0] <= t + slack:
                    if not slack or pts[i][1] == v:
                        return True
                    i += 1
                return False
        return False

    def _insert(self, key, t, v, note):
        pts = self.series.setdefault(key, [])
        if not pts or t >= pts[-1][0]:
            pts.append((t, v, note))
        else:                                           # late: a backlog, or health ahead of others
            pts.insert(bisect.bisect_right(pts, t, key=lambda p: p[0]), (t, v, note))
        if len(pts) > MAX_POINTS + 1000:                # trim in batches, not every message
            del pts[:-MAX_POINTS]
        if v is not None and key not in self.numeric:
            self.numeric.add(key)
            self.new_series = True
        if key in self.custom.graphed:
            self.custom.dirty = True

    def _reading(self, topic, t, reading):
        station, src = reading["station"], reading["source"]
        self._station_seen(station)
        info = self.readings.get((station, src))
        if info is None:
            info = self.readings[(station, src)] = {"topic": topic, "reading": reading, "t": t, "count": 0}
            if src not in KNOWN_SOURCES and not any(v.shows(src) for v in self.views):
                self._add_view(src)
        info["count"] += 1
        info["topic"] = topic
        if t >= info["t"]:                              # the newest by capture time
            info["reading"], info["t"] = reading, t
        keys = self.keys.setdefault((station, src), {"values": [], "meta": []})
        for section, seen in keys.items():
            for k, v in reading[section].items():
                if is_number(v) and k not in seen:
                    seen.append(k)
        if station == self.station():
            for view in self.views:
                if view.shows(src):
                    view.dirty = True

    def _tick(self):
        now = time.time()
        self._refresh_status(now)
        if self.new_series:
            self.custom.refresh_choices()
        page = self._current_page() if self.visible() else None
        if page is not None:
            page.refresh(now)
            if page.dirty:
                page.dirty = False
                page.draw(now)
        self.root.after(1000, self._tick)

    def _refresh_status(self, now):
        sub = self.sub
        if not sub:
            self.status_var.set("Enter the broker's address and the topics to subscribe to (e.g. "
                                "measure-station/+/+), then press Connect. Tick Simulated broker to "
                                "try it without one.")
            return
        if sub.csv.path:
            self.file_var.set("Logging to " + sub.csv.path)
        where = "simulated broker" if self.s["sim"] else sub.where
        link = "connected" if sub.connected else "connecting" if not sub.failed else "NOT CONNECTED (retrying)"
        station = self.station()
        self.status_var.set("   ".join(x for x in (
            "%s: %s" % (where, link), "messages %d" % sub.messages,
            "topics %d" % len(self.custom.topics),
            "last message %s ago" % ago(now - sub.last_rx) if sub.last_rx else "",
            "station %s: %s" % (station, self.station_status.get(station, "status not received"))
            if station else "") if x))

    def _clear_charts(self):
        for pts in self.series.values():
            pts.clear()
        self._add_log("info", "charts cleared (log files, topics and latest readings are unchanged)")
        self.custom.refresh_latest()
        self._mark_dirty()

    def _clear_all(self):
        self.custom.clear_topics()
        for d in (self.series, self.numeric, self.readings, self.keys, self.station_status):
            d.clear()
        self.custom.refresh_choices()
        self.custom.refresh_latest()
        self._mark_dirty()


GRID = {1: (1, 1), 2: (1, 2), 3: (1, 3), 4: (2, 2)}     # graphs in use -> (rows, cols); 5-6: 2 x 3


def _tk_dash(ls):
    """A matplotlib dash style as a Tk canvas dash pattern (None = solid)."""
    return None if ls == "-" else tuple(round(x) or 1 for x in ls[1])


class CustomView:
    """The Custom page: up to six values from any topic. Each goes on the graph
    numbered in its row; values given the same number share that graph, each
    in its own colour and dash with a legend. Only graphs in use are drawn."""

    def __init__(self, owner, notebook):
        self.owner = owner
        self.frame = ttk.Frame(notebook)
        self.dirty = True
        self.topics = {}            # topic -> {"count", "last"} (for the status line)
        self.overflow = False
        self.labels = {}            # series label -> (topic, field)
        self.graphed = set()        # (topic, field) shown on a graph
        self.layout = None          # graph numbers the figure is laid out for
        self.axes = []
        self._build()

    def _build(self):
        o = self.owner
        setup = ttk.Frame(self.frame, padding=(0, 8, 0, 0))
        setup.pack(fill="x")
        setup.columnconfigure(2, weight=1)
        for col, text in enumerate(("", "", "Value", "Graph", "Name", "Unit", "Scale", "Latest")):
            ttk.Label(setup, text=text, style="Status.TLabel").grid(row=0, column=col, sticky="w", padx=3)
        bg = ttk.Style().lookup("TFrame", "background") or PAGE
        self.rows = []
        for i, g in enumerate(o.s["graphs"]):
            r = {k: tk.StringVar() for k in ("series", "chart", "name", "unit", "scale", "latest")}
            r["series"].set(NONE if g["topic"] is None else series_label(g["topic"], g["field"]))
            r["chart"].set(str(g["chart"]))
            r["name"].set(g["name"])
            r["unit"].set(g["unit"])
            r["scale"].set("%g" % g["scale"])
            ttk.Label(setup, text="Value %d" % (i + 1)).grid(row=i + 1, column=0, sticky="w", padx=(0, 4))
            # This row's colour and dash, as drawn whenever it shares a graph.
            swatch = tk.Canvas(setup, width=36, height=12, highlightthickness=0, background=bg)
            swatch.create_line(2, 6, 34, 6, fill=CATEGORICAL[i], width=2.5, dash=_tk_dash(DASHES[i]))
            swatch.grid(row=i + 1, column=1, padx=(0, 6))
            r["box"] = ttk.Combobox(setup, textvariable=r["series"], state="readonly", width=50)
            r["box"].grid(row=i + 1, column=2, padx=3, pady=1, sticky="ew")
            r["box"].bind("<<ComboboxSelected>>", lambda e, r=r: self._series_chosen(r))
            chart = ttk.Combobox(setup, textvariable=r["chart"], state="readonly", width=3,
                                 values=[str(n) for n in range(1, MAX_GRAPHS + 1)])
            chart.grid(row=i + 1, column=3, padx=3)
            chart.bind("<<ComboboxSelected>>", lambda e: self.graphs_changed())
            ttk.Entry(setup, textvariable=r["name"], width=18).grid(row=i + 1, column=4, padx=3)
            ttk.Combobox(setup, textvariable=r["unit"], values=UNITS, width=6).grid(row=i + 1, column=5, padx=3)
            ttk.Entry(setup, textvariable=r["scale"], width=7).grid(row=i + 1, column=6, padx=3)
            ttk.Label(setup, textvariable=r["latest"], width=16).grid(row=i + 1, column=7, sticky="w", padx=3)
            for key in ("name", "unit", "scale"):
                r[key].trace_add("write", lambda *a: self.graphs_changed())
            self.rows.append(r)

        self.fig = Figure(figsize=(12, 5), facecolor=PAGE, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=(6, 0))
        self.canvas.mpl_connect("motion_notify_event", o._on_hover)

    # topics (counted for the status line) ---------------------------------------
    def topic_message(self, now, topic, text, retained):
        """Counts a message; False if there are too many topics to track."""
        t = self.topics.get(topic)
        if t is None:
            if len(self.topics) >= MAX_TOPICS:
                if not self.overflow:
                    self.overflow = True
                    self.owner._add_log("warning", "more than %d topics: further topics are still "
                                                   "logged but not shown; subscribe to fewer" % MAX_TOPICS)
                return False
            t = self.topics[topic] = {"count": 0}
        t["count"] += 1
        t["last"] = now
        return True

    def clear_topics(self):
        self.topics.clear()
        self.overflow = False

    def refresh(self, now):
        self.refresh_latest()

    def refresh_latest(self):
        for r, g in zip(self.rows, self.owner.s["graphs"]):
            if g["topic"] is None:
                r["latest"].set("")
                continue
            pts = self.owner.series.get((g["topic"], g["field"]), ())
            v = next((p[1] for p in reversed(pts) if p[1] is not None), None)
            r["latest"].set("-" if v is None else "%.2f %s" % (v * g["scale"], g["unit"]))

    # graph setup ----------------------------------------------------------------
    def refresh_choices(self):
        """The value lists: every series with a number, plus any saved value
        that has not arrived yet, so it stays selected."""
        self.labels = {series_label(*k): k for k in self.owner.numeric}
        for k in self.graphed:
            self.labels.setdefault(series_label(*k), k)
        values = [NONE] + sorted(self.labels)
        for r in self.rows:
            r["box"]["values"] = values
        self.owner.new_series = False

    def _series_chosen(self, r):
        # Name a value after its field, unless it already has a name.
        key = self.labels.get(r["series"].get())
        if key and not r["name"].get().strip():
            topic, field = key
            r["name"].set(field.rsplit(".", 1)[-1] or topic.rsplit("/", 1)[-1])
        self.graphs_changed()

    def graphs_changed(self, save=True):
        o = self.owner
        if o.loading:
            return
        for r, g in zip(self.rows, o.s["graphs"]):
            label = r["series"].get()
            if label == NONE:
                g["topic"], g["field"] = None, ""
            elif label in self.labels:
                g["topic"], g["field"] = self.labels[label]
            g["chart"] = int(r["chart"].get())
            g["name"] = r["name"].get().strip()
            g["unit"] = r["unit"].get().strip()
            try:
                g["scale"] = float(r["scale"].get())      # keep the last good value while typing
            except ValueError:
                pass
        self.graphed = {(g["topic"], g["field"]) for g in o.s["graphs"] if g["topic"] is not None}
        if save:
            o._save()
        self.refresh_choices()
        self.refresh_latest()
        self.dirty = True

    # charts -------------------------------------------------------------------
    def _lay_out(self, charts):
        for ax in self.axes:
            self.owner.hover.pop(ax, None)
        self.fig.clear()
        self.axes = []
        self.layout = charts
        if not charts:
            self.fig.text(0.5, 0.5, "Choose a value above to draw a graph. Values given the same "
                                    "graph number are drawn together.",
                          ha="center", va="center", color=MUTED, fontsize=10)
            return
        rows, cols = GRID.get(len(charts), (2, 3))
        for k, ax in enumerate(self.fig.subplots(rows, cols, squeeze=False).flat):
            if k < len(charts):
                self.axes.append(ax)
            else:
                ax.set_visible(False)

    def draw(self, now):
        o = self.owner
        graphs = o.s["graphs"]
        groups = {}                 # graph number -> value rows on it
        for i, g in enumerate(graphs):
            if g["topic"] is not None:
                groups.setdefault(g["chart"], []).append(i)
        charts = sorted(groups)
        if charts != self.layout:
            self._lay_out(charts)
        if not charts:
            self.canvas.draw_idle()
            return
        series = {i: o.series.get((graphs[i]["topic"], graphs[i]["field"]), [])
                  for rows in groups.values() for i in rows}
        end = o.view_end(series.values(), now)
        span = o.span()
        t0 = end - span if span else None
        firsts = [p[0][0] for p in series.values() if p]
        xlim = o._xlim(min(firsts), t0, end) if firsts else None
        for ax, c in zip(self.axes, charts):
            ax.clear()
            o.hover.pop(ax, None)
            rows = groups[c]
            window = {i: [p for p in series[i] if t0 is None or p[0] >= t0] for i in rows}
            if len(rows) == 1:
                i = rows[0]
                g = graphs[i]
                label = series_label(g["topic"], g["field"])
                if len(label) > 40:
                    label = "..." + label[-37:]
                name = g["name"] or "Value %d" % (i + 1)
                o._style(ax, "%s%s   (%s)" % (name, "  -  " + g["unit"] if g["unit"] else "", label))
                if not window[i] or xlim is None:
                    o._waiting(ax, "waiting for messages" if o.sub else "no messages for this value")
                    continue
                o.plot(ax, window[i], g["unit"], xlim, 9, g["scale"], CATEGORICAL[i])
                continue

            # Shared graph: one y scale for all its lines (never a second axis).
            units = []
            for i in rows:
                if graphs[i]["unit"] not in units:
                    units.append(graphs[i]["unit"])
            shown = [u for u in units if u]
            title = "Graph %d" % c + ("  -  " + " / ".join(shown) if shown else "")
            if len(units) > 1:
                title += "   (mixed units on one scale)"
            o._style(ax, title)
            if not any(window.values()) or xlim is None:
                o._waiting(ax, "waiting for messages" if o.sub else "no messages for these values")
                continue
            handles, hover, values = [], [], []
            for i in rows:
                g = graphs[i]
                name = g["name"] or "Value %d" % (i + 1)
                pts = (o.plot(ax, window[i], g["unit"], xlim, None, g["scale"], CATEGORICAL[i], DASHES[i],
                              alone=False) if window[i] else [])
                values += [v for t, v, n in pts if v is not None]
                latest = next((v for t, v, n in reversed(pts) if v is not None), None)
                handles.append(Line2D([], [], color=CATEGORICAL[i], ls=DASHES[i], lw=2, label="%s   %s" % (
                    name, "-" if latest is None else "%.2f %s" % (latest, g["unit"]))))
                hover.append((name, g["unit"], pts))
            if o.fixed_var.get() and values:
                o._zero_based(ax, values)
            ax.legend(handles=handles, loc="upper left", fontsize=8, labelcolor=INK_2, handlelength=3.2,
                      frameon=True, facecolor=SURFACE, edgecolor="none", framealpha=0.85)
            o._set_hover_shared(ax, hover)
        o.tip = None
        self.canvas.draw_idle()
