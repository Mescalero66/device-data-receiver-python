"""MQ gas sensor board receiver: live display and CSV logging.

    python mqb_receiver.py                      GUI, choose the port in the window
    python mqb_receiver.py --port COM7          GUI, connect straight away
    python mqb_receiver.py --cli --port COM7    console only
    python mqb_receiver.py --sim                GUI fed by a simulated board
    python mqb_receiver.py --modbus-sim         Modbus tab fed by a simulated sensor
    python mqb_receiver.py --mqtt-sim           MQTT tab fed by a simulated broker

New readings are appended to data/mqb_YYYY-MM-DD.csv (simulator runs go to
data/sim/). See README.md for details.
"""

import argparse
import os
import queue
import sys
import time
from datetime import datetime

from mqb_core import (HealthWatch, Receiver, SerialSource, SimSource, list_ports, merge_points,
                      read_csv_logs)
from mqb_protocol import CLEAN_AIR_PPM, CYCLE_S, GAS, GAS_NAME, SENSORS

HERE = os.path.dirname(os.path.abspath(__file__))
SIM_LABEL = "Simulator (no hardware)"


def make_source(port, args, on_event):
    if port is None:
        if args.sim_faults:
            return SimSource(args.sim_speed, saved_baseline=False, fault_rate=0.05,
                             corrupt_rate=0.02, failed_calibration=("MQ4",),
                             restart_after=900)
        return SimSource(args.sim_speed, start_uptime=86400)
    return SerialSource(port, on_event)


def out_folder(args, port):
    return os.path.join(args.out, "sim") if port is None else args.out


def fmt_age(seconds):
    s = int(seconds)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % divmod(s, 60)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return ("%dd %dh %02dm" % (d, h, s // 60)) if d else ("%dh %02dm" % (h, s // 60))


def fmt_num(v, spec):
    return "-" if v is None else spec % v


def describe(r):
    s = r.sentence
    return "%s %-5s %-3s t_s=%-7d ppm=%-8s temp=%-6s r0=%s%s" % (
        datetime.fromtimestamp(r.rx_time).strftime("%H:%M:%S"), s.name, s.gas, s.t_s,
        fmt_num(s.ppm, "%.2f"), fmt_num(s.temp_c, "%.2f"), fmt_num(s.r0, "%.6f"),
        ("  [" + " ".join(r.flags) + "]") if r.flags else "")


# --- console mode -------------------------------------------------------------

def run_cli(args):
    port = None if args.sim else args.port
    if port is None and not args.sim:
        print("--cli needs --port (or --sim). Ports found:")
        for dev, desc in list_ports():
            print("  %-6s %s" % (dev, desc))
        return 2

    def emit(kind, payload):
        if kind == "reading":
            print(describe(payload), flush=True)
        elif kind == "log":
            level, text = payload
            print("%s [%s] %s" % (datetime.now().strftime("%H:%M:%S"), level, text), flush=True)

    rx = Receiver(make_source(port, args, lambda lv, t: emit("log", (lv, t))), emit,
                  out_folder(args, port), raw_log=args.raw)
    watch = HealthWatch(rx.tracker, time.time())
    print("Logging new readings to %s  (Ctrl+C to stop)" % os.path.abspath(out_folder(args, port)))
    rx.start()
    try:
        while rx.thread.is_alive():
            time.sleep(1)
            for level, text in watch.check(time.time()):
                emit("log", (level, text))
    except KeyboardInterrupt:
        pass
    rx.stop()
    st = rx.stats
    print("lines %d, valid %d, rejected %d, with warnings %d, readings logged %d, restarts %d"
          % (st.lines, st.valid, st.rejected, st.warnings, st.readings, rx.tracker.restarts))
    return 0


# --- GUI --------------------------------------------------------------------------

MAX_HISTORY = 20000             # readings kept per sensor for plotting (~35 days)


def run_gui(args):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from gui_common import (CRITICAL, INK_2, MUTED, PAGE, SERIES, SURFACE, WINDOWS, TabBase)
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from modbus_tab import ModbusTab
    from mqtt_core import is_mqtt_log
    from mqtt_tab import MqttTab

    # CSV logs given on the command line (or dropped onto run.bat) go to the
    # tab that wrote them: MQTT logs to the MQTT tab, the rest to UART.
    mqtt_csv = [p for p in args.csv if is_mqtt_log(p)]
    args.csv = [p for p in args.csv if p not in mqtt_csv]

    class UartTab(TabBase):
        def __init__(self, root, frame, notebook):
            super().__init__(root, frame, notebook)
            self.rx = None
            self.watch = None
            self.port = None
            self.history = {n: [] for n in SENSORS}     # (rx_time, ppm, tracking, temp_c)
            self.restarts = []
            self.latest = {}
            self._build()
            self._refresh_ports()
            if args.csv:
                self._import(args.csv)
            if args.sim:
                self.port_var.set(SIM_LABEL)
                self._connect()
            elif args.port:
                self.port_var.set(next((d for d in self.port_box["values"]
                                        if d.split()[0] == args.port), args.port))
                self._connect()
            root.after(200, self._poll)
            root.after(1000, self._tick)

        # layout -------------------------------------------------------
        def _build(self):
            frame = self.frame
            bar = ttk.Frame(frame, padding=(8, 8, 8, 4))
            bar.pack(fill="x")
            ttk.Label(bar, text="Port").pack(side="left")
            self.port_var = tk.StringVar()
            self.port_box = ttk.Combobox(bar, textvariable=self.port_var, width=52, state="readonly")
            self.port_box.pack(side="left", padx=(6, 4))
            ttk.Button(bar, text="Refresh", command=self._refresh_ports).pack(side="left")
            self.conn_btn = ttk.Button(bar, text="Connect", command=self._toggle)
            self.conn_btn.pack(side="left", padx=(8, 0))
            self.raw_var = tk.BooleanVar(value=args.raw)
            ttk.Checkbutton(bar, text="Also log raw lines", variable=self.raw_var).pack(side="left", padx=12)
            ttk.Button(bar, text="Open log folder", command=self._open_folder).pack(side="right")
            self.file_var = tk.StringVar(value="Not connected")
            ttk.Label(bar, textvariable=self.file_var, style="Status.TLabel").pack(side="right", padx=8)

            self.status_var = tk.StringVar(value="")
            ttk.Label(frame, textvariable=self.status_var, style="Status.TLabel",
                      padding=(8, 0, 8, 4)).pack(fill="x")

            # The table takes the width of two chart columns and the board
            # temperature chart the third, both as tall as the table's 6 rows.
            top = ttk.Frame(frame)
            top.pack(fill="x", padx=8)
            top.columnconfigure(0, weight=2, uniform="top")
            top.columnconfigure(1, weight=1, uniform="top")
            table_box = ttk.Frame(top)
            table_box.grid(row=0, column=0, sticky="nsew")

            cols = [("sensor", "Sensor", 50), ("gas", "Gas", 130), ("ppm", "ppm", 58),
                    ("ratio", "vs clean air", 70), ("temp", "Temp C", 50),
                    ("tracking", "R0 basis", 95), ("r0", "R0 kOhm", 64), ("t_s", "t_s", 54),
                    ("age", "Since reading", 84), ("status", "Status", 200)]
            self.tree = ttk.Treeview(table_box, columns=[c[0] for c in cols], show="headings", height=6)
            for key, title, width in cols:
                self.tree.heading(key, text=title)
                anchor = "e" if key in ("ppm", "ratio", "temp", "r0", "t_s") else "w"
                self.tree.column(key, width=width, anchor=anchor, stretch=key == "status")
            self.tree.tag_configure("bad", background="#fbe3e3")
            self.tree.tag_configure("warn", background="#fdf0d0")
            for n in SENSORS:
                self.tree.insert("", "end", iid=n, values=(n,))
            self.tree.pack(fill="both", expand=True)
            # Size the row by the table's height only; the columns' widths come
            # from the 2:1 split, not from what the table or chart request.
            table_box.update_idletasks()
            table_box.configure(width=1, height=self.tree.winfo_reqheight())
            table_box.pack_propagate(False)

            self.temp_fig = Figure(figsize=(4, 1.7), facecolor=PAGE, layout="constrained")
            self.temp_ax = self.temp_fig.subplots()
            self.temp_canvas = FigureCanvasTkAgg(self.temp_fig, master=top)
            self.temp_canvas.get_tk_widget().configure(width=1, height=1)
            self.temp_canvas.get_tk_widget().grid(row=0, column=1, sticky="nsew", padx=(8, 0))
            self.temp_canvas.mpl_connect("motion_notify_event", self._on_hover)

            panes = ttk.PanedWindow(frame, orient="vertical")
            panes.pack(fill="both", expand=True, padx=8, pady=8)

            plot_frame = ttk.Frame(panes)
            ctl = ttk.Frame(plot_frame)
            ctl.pack(fill="x")
            ttk.Label(ctl, text="Show").pack(side="left")
            self.window_var = tk.StringVar(value="6 hours")
            box = ttk.Combobox(ctl, textvariable=self.window_var, state="readonly", width=10,
                               values=[w[0] for w in WINDOWS])
            box.pack(side="left", padx=6)
            box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
            self.log_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(ctl, text="Log scale", variable=self.log_var,
                            command=lambda: self._set_scale(self.log_var, self.fixed_var)
                            ).pack(side="left", padx=8)
            ttk.Button(ctl, text="Import CSV...", command=self._import).pack(side="left", padx=(8, 4))
            ttk.Button(ctl, text="Clear charts", command=self._clear_charts).pack(side="left")
            self._events_checkbox(ctl).pack(side="left", padx=12)
            self.fixed_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(ctl, text="Fix axis", variable=self.fixed_var,
                            command=lambda: self._set_scale(self.fixed_var, self.log_var)
                            ).pack(side="left")
            ttk.Label(ctl, text="Hover a chart for values. Plotted at receive time (within ~10s of the reading).",
                      style="Status.TLabel").pack(side="right")

            self.fig = Figure(figsize=(12, 5), facecolor=PAGE, layout="constrained")
            self.axes = self.fig.subplots(2, 3)
            self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.canvas.mpl_connect("motion_notify_event", self._on_hover)
            self.fig.legend(handles=[
                Line2D([], [], color=SERIES, lw=2, label="ppm"),
                Line2D([], [], color=SERIES, lw=0, marker="o", markersize=7, markerfacecolor=SURFACE,
                       markeredgewidth=1.5, label="provisional (boot calibration R0)"),
                Line2D([], [], color=CRITICAL, lw=0, marker="x", markersize=7, markeredgewidth=1.5,
                       label="reading with no ppm"),
                Line2D([], [], color=MUTED, lw=1, ls=(0, (4, 3)), label="clean-air level"),
                Line2D([], [], color=MUTED, lw=1, ls=":", label="board restart"),
            ], loc="outside upper center", ncols=5, frameon=False, fontsize=9, labelcolor=INK_2)
            panes.add(plot_frame, weight=4)
            self._build_events(panes)

        # connection ---------------------------------------------------
        def _refresh_ports(self):
            values = ["%s  -  %s" % (dev, desc) for dev, desc in list_ports()] + [SIM_LABEL]
            self.port_box["values"] = values
            if self.port_var.get() not in values:
                self.port_var.set(values[0])

        def _toggle(self):
            if self.rx:
                self._disconnect()
            else:
                self._connect()

        def _connect(self):
            choice = self.port_var.get()
            self.port = None if choice == SIM_LABEL else choice.split()[0]
            emit = lambda kind, payload: self.q.put((kind, payload))
            source = make_source(self.port, args, lambda lv, t: emit("log", (lv, t)))
            self.rx = Receiver(source, emit, out_folder(args, self.port), raw_log=self.raw_var.get())
            self.watch = HealthWatch(self.rx.tracker, time.time())
            self.latest.clear()
            self.rx.start()
            self.conn_btn.configure(text="Disconnect")
            self.port_box.configure(state="disabled")
            self.file_var.set("Logging to " + os.path.abspath(out_folder(args, self.port)))

        def _disconnect(self):
            self.rx.stop()
            self._drain()
            self._add_log("info", "disconnected")
            self.rx = None
            self.conn_btn.configure(text="Connect")
            self.port_box.configure(state="readonly")
            self.file_var.set("Not connected")

        def _open_folder(self):
            folder = out_folder(args, self.port) if self.rx else args.out
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)

        def shutdown(self):
            if self.rx:
                self.rx.stop()

        # imported logs ------------------------------------------------
        def _import(self, paths=None):
            if paths is None:
                paths = filedialog.askopenfilenames(
                    parent=self.root, title="Import CSV logs",
                    initialdir=os.path.abspath(out_folder(args, self.port) if self.rx else args.out),
                    filetypes=[("CSV logs", "*.csv"), ("All files", "*.*")])
                if not paths:
                    return
            try:
                points, restarts, skipped = read_csv_logs(paths)
            except (OSError, ValueError) as e:
                messagebox.showerror("Import CSV", str(e), parent=self.root)
                return
            # Merge in time order, skipping readings already on the charts.
            added = 0
            for n, new in points.items():
                added += merge_points(self.history[n], new)
                del self.history[n][:-MAX_HISTORY]
            # A live restart is logged on the first empty sentence, an imported
            # one at the first reading after it, so allow for the difference.
            for rt in restarts:
                if all(abs(rt - r) > CYCLE_S for r in self.restarts):
                    self.restarts.append(rt)
            self.restarts.sort()

            names = ", ".join(os.path.basename(p) for p in paths)
            times = [p[0] for pts in points.values() for p in pts]
            if not times:
                self._add_log("warning", "no readings found in %s" % names)
                return
            total = len(times)
            text = "imported %d readings from %s (%s to %s)" % (
                added, names, datetime.fromtimestamp(min(times)).strftime("%d %b %H:%M"),
                datetime.fromtimestamp(max(times)).strftime("%d %b %H:%M"))
            if added < total:
                text += ", %d already shown" % (total - added)
            if restarts:
                text += ", %d board restart(s)" % len(restarts)
            if skipped:
                text += ", %d unreadable row(s) skipped" % skipped
            self._add_log("info", text)

            # Widen the chart window if the imported data would not fit.
            need = self._view_end(time.time()) - min(times)
            span = dict(WINDOWS)[self.window_var.get()]
            if span is not None and span < need:
                self.window_var.set(next(w for w, s in WINDOWS if s is None or s >= need))
            self._mark_dirty()

        def _clear_charts(self):
            for h in self.history.values():
                h.clear()
            self.restarts.clear()
            self._add_log("info", "charts cleared (log files are unchanged)")
            self._mark_dirty()

        def _view_end(self, now):
            """Charts end at the present while connected, else at the newest data."""
            if self.rx:
                return now
            last = [h[-1][0] for h in self.history.values() if h]
            return max(last) if last else now

        # data flow ----------------------------------------------------
        def _poll(self):
            self._drain()
            self.root.after(200, self._poll)

        def _drain(self):
            changed = False
            try:
                for _ in range(1000):
                    kind, payload = self.q.get_nowait()
                    if kind == "sentence":
                        s, now = payload
                        self.latest[s.name] = s
                        changed = True
                    elif kind == "reading":
                        s = payload.sentence
                        h = self.history[s.name]
                        h.append((payload.rx_time, s.ppm, s.tracking, s.temp_c))
                        del h[:-MAX_HISTORY]
                        self.plot_dirty = True
                    elif kind == "log":
                        level, text = payload
                        if level == "restart":
                            self.restarts.append(time.time())
                            self.latest.clear()
                        self._add_log(level, text)
            except queue.Empty:
                pass
            if changed:
                self._refresh_table()

        def _tick(self):
            now = time.time()
            if self.watch:
                for level, text in self.watch.check(now):
                    self._add_log(level, text)
            self._refresh_table()
            self._refresh_status(now)
            if self.plot_dirty and self.visible():
                self.plot_dirty = False
                self._draw()
            self.root.after(1000, self._tick)

        def _set_scale(self, ticked, other):
            # A log axis cannot start at zero, so Log scale and Fix axis
            # are alternatives: ticking one unticks the other.
            if ticked.get():
                other.set(False)
            self._mark_dirty()

        # table & status -----------------------------------------------
        def _refresh_table(self):
            now = time.time()
            t = self.rx.tracker if self.rx else None
            down = t is None or t.link_down(now)
            stale = set(t.stale_sensors(now)) if t else set()
            for n in SENSORS:
                s = self.latest.get(n)
                gas = "%s (%s)" % (GAS[n], GAS_NAME[GAS[n]])
                if s is None:
                    status = "not connected" if t is None else "waiting for data"
                    self.tree.item(n, values=(n, gas, "", "", "", "", "", "", "", status), tags=())
                    continue
                since = t.since.get(n)
                age = fmt_age(now - since) if (since and s.t_s is not None) else ""
                ratio = "%.2fx" % (s.ppm / CLEAN_AIR_PPM[n]) if s.ppm is not None else ""
                basis = {True: "tracked baseline", False: "provisional", None: ""}[s.tracking]
                tag = ()
                if down:
                    status, tag = "LINK DOWN - showing last data", ("warn",)
                elif s.t_s is None:
                    status = "not read since boot yet"
                elif n in stale:
                    status, tag = "STALE - sensor not updating", ("warn",)
                elif s.ppm is None:
                    status, tag = "FAULT - read failed / no valid output", ("bad",)
                elif s.tracking is False and s.r0 == 10.0:
                    status, tag = "boot calibration failed (placeholder R0)", ("bad",)
                elif s.tracking is False:
                    status = "ok - provisional until baseline (~26h)"
                else:
                    status = "ok"
                if s.temp_c is None and s.t_s is not None:
                    status += "; no temp compensation"
                self.tree.item(n, tags=tag, values=(
                    n, gas, fmt_num(s.ppm, "%.2f"), ratio, fmt_num(s.temp_c, "%.2f"), basis,
                    fmt_num(s.r0, "%.6f"), fmt_num(s.t_s, "%d"), age, status))

        def _refresh_status(self, now):
            if not self.rx:
                self.status_var.set("Choose the USB-UART adapter's port and press Connect.")
                return
            t, st = self.rx.tracker, self.rx.stats
            if self.rx.csv.path:
                self.file_var.set("Logging to " + self.rx.csv.path)
            if t.last_rx is None:
                link = "waiting for data"
            elif t.link_down(now):
                link = "NO DATA for %s" % fmt_age(now - t.last_rx)
            else:
                link = "receiving"
            uptime = ("board uptime ~%s" % fmt_age(now - t.boot_offset)) if t.boot_offset else ""
            self.status_var.set("   ".join(x for x in (
                "%s: %s" % (self.rx.source.label, link), uptime,
                "lines %d" % st.lines, "valid %d" % st.valid, "rejected %d" % st.rejected,
                "with spec warnings %d" % st.warnings, "readings logged %d" % st.readings,
                "restarts seen %d" % t.restarts) if x))

        # charts -------------------------------------------------------
        def _draw(self):
            end = self._view_end(time.time())
            span = dict(WINDOWS)[self.window_var.get()]
            t0 = end - span if span else None
            firsts = [h[0][0] for h in self.history.values() if h]
            xlim = self._xlim(min(firsts), t0, end) if firsts else None
            self.hover.clear()
            for ax, n in zip(self.axes.flat, SENSORS):
                ax.clear()
                self._style(ax, "%s  -  %s ppm" % (n, GAS_NAME[GAS[n]]))
                pts = [p for p in self.history[n] if t0 is None or p[0] >= t0]
                ax.axhline(CLEAN_AIR_PPM[n], color=MUTED, lw=1, ls=(0, (4, 3)), zorder=1)
                self._restart_lines(ax, t0)
                if not pts:
                    self._waiting(ax)
                    continue
                self._line(ax, [(e, p) for e, p, trk, tc in pts], 2.5 * CYCLE_S)
                prov = [(datetime.fromtimestamp(e), p) for e, p, trk, tc in pts
                        if trk is False and p is not None]
                if prov:
                    ax.plot(*zip(*prov), lw=0, marker="o", markersize=6, color=SERIES,
                            markerfacecolor=SURFACE, markeredgewidth=1.5, zorder=4)
                faults = [datetime.fromtimestamp(e) for e, p, trk, tc in pts if p is None]
                if faults:
                    ax.plot(faults, [0.04] * len(faults), transform=ax.get_xaxis_transform(), lw=0,
                            marker="x", markersize=7, markeredgewidth=1.5, color=CRITICAL, zorder=4)
                valid = [(e, p) for e, p, trk, tc in pts if p is not None]
                self._latest(ax, valid, 9)
                if self.log_var.get():
                    ax.set_yscale("log")
                elif self.fixed_var.get():
                    # Zero-based, so steady readings look steady rather than
                    # small noise filling the whole height.
                    ax.set_ylim(0, max([p for e, p in valid] + [CLEAN_AIR_PPM[n]]) * 1.1)
                ax.set_xlim(*xlim)
                self._set_hover(ax, "ppm", [(e, p, trk) for e, p, trk, tc in pts], "no ppm (read fault)")
            self._draw_temperature(t0, xlim)
            self.tip = None
            self.canvas.draw_idle()
            self.temp_canvas.draw_idle()

        def _draw_temperature(self, t0, xlim):
            # The board has one temperature sensor and every sentence carries
            # its value, so all sensors' readings together make one series.
            ax = self.temp_ax
            ax.clear()
            self._style(ax, "Board temperature  -  \N{DEGREE SIGN}C")
            pts = sorted((e, tc) for h in self.history.values() for e, p, trk, tc in h
                         if tc is not None and (t0 is None or e >= t0))
            self._restart_lines(ax, t0)
            if not pts:
                self._waiting(ax)
                return
            self._line(ax, pts, 2.5 * CYCLE_S)
            self._latest(ax, pts, 8)
            if self.fixed_var.get():
                # Log scale does not apply here: temperatures can be zero or below.
                self._zero_based(ax, [tc for e, tc in pts])
            ax.set_xlim(*xlim)
            self._set_hover(ax, "\N{DEGREE SIGN}C", [(e, tc, None) for e, tc in pts])

        def _restart_lines(self, ax, t0):
            for rt in self.restarts:
                if t0 is None or rt >= t0:
                    ax.axvline(datetime.fromtimestamp(rt), color=MUTED, lw=1, ls=":")

    root = tk.Tk()
    root.title("Device Data Receiver")
    root.geometry("1320x900")
    root.minsize(900, 600)
    style = ttk.Style()
    style.configure("Treeview", rowheight=24)
    style.configure("Status.TLabel", foreground=INK_2)
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    uart_frame, modbus_frame, mqtt_frame = ttk.Frame(notebook), ttk.Frame(notebook), ttk.Frame(notebook)
    notebook.add(uart_frame, text="  UART  ")
    notebook.add(modbus_frame, text="  Modbus  ")
    notebook.add(mqtt_frame, text="  MQTT  ")
    tabs = [UartTab(root, uart_frame, notebook),
            ModbusTab(root, modbus_frame, notebook, args.out, args.modbus_settings,
                      connect=args.modbus, sim=args.modbus_sim),
            MqttTab(root, mqtt_frame, notebook, args.out, args.mqtt_settings,
                    connect=args.mqtt, sim=args.mqtt_sim, csv=mqtt_csv)]
    # Show the tab that was asked to connect or given logs; UART first, then
    # Modbus, then MQTT.
    if not (args.sim or args.port or args.csv):
        if args.modbus or args.modbus_sim:
            notebook.select(modbus_frame)
        elif args.mqtt or args.mqtt_sim or mqtt_csv:
            notebook.select(mqtt_frame)

    def close():
        for tab in tabs:
            tab.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Receive, display and log MQ gas sensor board broadcasts (UART), "
                                            "Modbus TCP devices and MQTT topics.")
    p.add_argument("csv", nargs="*", help="CSV logs from earlier runs to show on the charts (window only)")
    p.add_argument("--port", help="serial port, e.g. COM7")
    p.add_argument("--cli", action="store_true", help="console only, no window")
    p.add_argument("--out", default=os.path.join(HERE, "data"), help="folder for CSV logs (default: ./data)")
    p.add_argument("--raw", action="store_true", help="also log every received line to mqb_raw_<date>.log")
    p.add_argument("--sim", action="store_true", help="use a simulated board instead of a serial port")
    p.add_argument("--sim-speed", type=float, default=10, help="simulator time acceleration (default 10)")
    p.add_argument("--modbus", action="store_true",
                   help="start polling the Modbus device saved in the Modbus tab's settings")
    p.add_argument("--modbus-sim", action="store_true",
                   help="start the Modbus tab on a simulated environment sensor")
    p.add_argument("--modbus-settings", default=os.path.join(HERE, "modbus_settings.json"),
                   help="Modbus tab settings file (default: ./modbus_settings.json)")
    p.add_argument("--mqtt", action="store_true",
                   help="start subscribing to the MQTT broker saved in the MQTT tab's settings")
    p.add_argument("--mqtt-sim", action="store_true",
                   help="start the MQTT tab on a simulated broker")
    p.add_argument("--mqtt-settings", default=os.path.join(HERE, "mqtt_settings.json"),
                   help="MQTT tab settings file (default: ./mqtt_settings.json)")
    p.add_argument("--sim-faults", action="store_true",
                   help="simulate a fresh board with read faults, corrupted lines, a failed MQ4 "
                        "boot calibration and a restart at 15 min uptime")
    args = p.parse_args(argv)
    if args.cli and args.csv:
        p.error("CSV import is only available in the window, not with --cli")
    return run_cli(args) if args.cli else run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
