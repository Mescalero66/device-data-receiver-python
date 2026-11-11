"""One sensor's page under the MQTT tab: a status table (one row per source)
and its charts, large for the numbers in `values`, small for those in `meta`.

The page works out its graphs from the readings themselves, so a SEN62 and a
SEN65 each get the graphs for what they send, and unknown keys still appear.
The data lives in the MqttTab (`owner`); this only lays it out and draws it.
"""

import math
from datetime import datetime
from tkinter import ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from gui_common import PAGE
from station_spec import (HEALTH_LARGE, ago, assess, details, is_large, label, scale,
                          shared_keys, source_title, unit)

LARGE_ROW, SMALL_ROW = 2.6, 1.5     # relative heights of a row of large / small graphs
SMALL_COLS = 6                      # small graphs per row (empty slots keep them small)


class SensorView:
    def __init__(self, owner, notebook, title, sources, interval):
        self.owner, self.title, self.sources, self.interval = owner, title, list(sources), interval
        self.shared = shared_keys(sources)
        self.frame = ttk.Frame(notebook)
        self.dirty = True
        self.layout = None          # (large graphs, small graphs) the figure is laid out for
        self.axes = []              # [(ax, (source, section, key), small)]

        cols = [("source", "Source", 110), ("sensor", "Sensor", 90), ("time", "Last reading", 120),
                ("age", "Since", 50), ("ts_src", "Time source", 80), ("count", "Readings", 64),
                ("details", "Details", 330), ("status", "Status", 330)]
        box = ttk.Frame(self.frame)
        box.pack(fill="x", pady=(6, 4))
        self.tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings",
                                 height=len(self.sources))
        for key, text, width in cols:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=40, stretch=key in ("details", "status"),
                             anchor="e" if key in ("age", "count") else "w")
        self.tree.tag_configure("bad", background="#fbe3e3")
        self.tree.tag_configure("warn", background="#fdf0d0")
        for s in self.sources:
            self.tree.insert("", "end", iid=s, values=(s,))
        self.tree.pack(fill="x")

        self.fig = Figure(figsize=(12, 5), facecolor=PAGE, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("motion_notify_event", owner._on_hover)

    def shows(self, source):
        return source in self.sources

    # status table -------------------------------------------------------------
    def refresh(self, now):
        o = self.owner
        station = o.station()
        for src in self.sources:
            info = o.readings.get((station, src))
            if info is None:
                waiting = "waiting for readings" if o.sub else "not connected"
                self.tree.item(src, tags=(), values=(src, "", "", "", "", "", "", waiting))
                continue
            r = info["reading"]
            t = info["t"]
            level, status = assess(src, r, now - t, self.interval, o.station_status.get(station))
            self.tree.item(src, tags=(level,) if level != "ok" else (), values=(
                src, r["sensor_type"], datetime.fromtimestamp(t).strftime("%d %b %H:%M:%S"),
                ago(now - t), r["ts_src"] or "-", info["count"],
                details(r, o.keys.get((station, src), {})), status))

    # charts ---------------------------------------------------------------------
    def _graphs(self, station):
        """[(source, section, key)] large and small, in a stable order:
        sources as listed, then keys in the order they first arrived."""
        large, small, shared = [], [], set()
        for src in self.sources:
            keys = self.owner.keys.get((station, src))
            if not keys:
                continue
            for section in ("values", "meta"):
                for k in keys[section]:
                    if (section, k) in self.shared:
                        if (section, k) in shared:
                            continue            # one graph for every source's copy
                        shared.add((section, k))
                    (large if is_large(src, section, k) else small).append((src, section, k))
        if self.sources == ["health"]:
            large.sort(key=lambda g: HEALTH_LARGE.index(g[2]))
        return large, small

    def _lay_out(self, large, small):
        for ax, _, _ in self.axes:
            self.owner.hover.pop(ax, None)
        self.fig.clear()
        self.axes = []
        self.layout = (large, small)
        nl, ns = len(large), len(small)
        if not nl and not ns:
            return
        cols_l = 1 if nl == 1 else 2 if nl in (2, 4) else 3 if nl <= 9 else 4
        rows_l = math.ceil(nl / cols_l) if nl else 0
        # One row if they fit (up to 8), else two or three rows.
        cols_s = max(SMALL_COLS, ns if ns <= 8 else math.ceil(ns / 2) if ns <= 16 else math.ceil(ns / 3))
        rows_s = math.ceil(ns / cols_s) if ns else 0
        if nl and ns:
            top, bottom = self.fig.subfigures(2, 1, height_ratios=[rows_l * LARGE_ROW, rows_s * SMALL_ROW])
        else:
            top = bottom = self.fig
        for fig, graphs, rows, cols, small in ((top, large, rows_l, cols_l, False),
                                               (bottom, small, rows_s, cols_s, True)):
            if not graphs:
                continue
            fig.set_facecolor(PAGE)
            grid = fig.subplots(rows, cols, squeeze=False).flat
            for i, ax in enumerate(grid):
                if i < len(graphs):
                    self.axes.append((ax, graphs[i], small))
                else:
                    ax.set_visible(False)

    def draw(self, now):
        o = self.owner
        station = o.station()
        large, small = self._graphs(station)
        if self.layout != (large, small):
            self._lay_out(large, small)
        if not self.axes:
            self.fig.clear()
            ax = self.fig.subplots()
            o._style(ax, self.title)
            o._waiting(ax, "no station yet" if not station else "waiting for readings from %s / %s"
                       % (station, ", ".join(self.sources)) if o.sub else
                       "no readings from %s / %s" % (station, ", ".join(self.sources)))
            self.layout = None              # lay out again once graphs exist
            self.canvas.draw_idle()
            return
        series = {g: o.points(station, *g) for _, g, _ in self.axes}
        end = o.view_end(series.values(), now)
        span = o.span()
        t0 = end - span if span else None
        firsts = [p[0][0] for p in series.values() if p]
        xlim = o._xlim(min(firsts), t0, end) if firsts else None
        many = len(self.sources) > 1
        for ax, g, small in self.axes:
            src, section, key = g
            ax.clear()
            o.hover.pop(ax, None)
            info = o.readings.get((station, src))
            name, u = label(key), unit(key)
            if (section, key) in self.shared:
                name = self.shared[(section, key)]
            elif many:
                title = source_title(src, info and info["reading"])
                if small:                       # several sources: six small graphs a row
                    title, name = title.split()[0], name.replace("Temperature", "Temp")
                name = "%s  %s" % (title, name)
            if small:
                # Too small for a newest-value label, so it goes in the title.
                v = next((p[1] for p in reversed(series[g]) if p[1] is not None), None)
                o._style(ax, name + ("" if v is None else "   %.2f %s" % (v * scale(key), u)))
                ax.title.set_fontsize(8)
                ax.tick_params(labelsize=7)
                ax.xaxis.get_major_formatter().show_offset = False
            else:
                o._style(ax, name + ("  -  " + u if u else ""))
            pts = [p for p in series[g] if t0 is None or p[0] >= t0]
            if not pts or xlim is None:
                o._waiting(ax, "no readings in this window")
                continue
            o.plot(ax, pts, u, xlim, None if small else 9, scale(key))
        o.tip = None
        self.canvas.draw_idle()
