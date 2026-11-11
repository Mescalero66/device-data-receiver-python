"""Window pieces shared by the UART and Modbus tabs: theme, Events pane,
chart styling and hover. Imported only by the GUI, so console mode does not
need Tk or matplotlib.
"""

import math
import queue
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.dates as mdates                       # noqa: E402

# Light theme tokens (chart surface, inks, hairlines) and the series/status hues.
SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES = "#2a78d6"
CRITICAL = "#d03b3b"
LOG_COLORS = {"error": CRITICAL, "reject": CRITICAL, "restart": "#4a3aa7",
              "warning": "#9a6a00", "notice": INK_2, "info": INK}
WINDOWS = [("1 hour", 3600), ("6 hours", 6 * 3600), ("24 hours", 86400),
           ("7 days", 7 * 86400), ("All", None)]

# Lines that share a chart: categorical slots 1-6 of the validated reference
# palette, in its fixed order (slot 1 is SERIES). Slots 1-3 are told apart by
# every kind of colour vision for any pair; 4-6 only beside their neighbours,
# so shared charts also give each line its own dash (DASHES) and a legend.
CATEGORICAL = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
DASHES = ("-", (0, (6, 3)), (0, (1.5, 2.5)), (0, (6, 2, 1.5, 2)), (0, (12, 3)), (0, (3, 1.5)))
HOVER_MATCH_S = 120             # a shared chart's tooltip lists values within this of the time pointed at


class TabBase:
    """One notebook tab: a worker reports through self.q, the tab drains it
    on the Tk thread, and charts are redrawn only while the tab is showing."""

    def __init__(self, root, frame, notebook):
        self.root, self.frame, self.notebook = root, frame, notebook
        self.q = queue.Queue()
        self.hover = {}
        self.tip = None
        self.plot_dirty = True
        self.unseen = 0                 # events logged while the pane is hidden
        notebook.bind("<<NotebookTabChanged>>", lambda e: self._mark_dirty(), add="+")

    def visible(self):
        return self.notebook.select() == str(self.frame)

    def _mark_dirty(self):
        self.plot_dirty = True

    # events ---------------------------------------------------------
    def _events_checkbox(self, parent):
        self.events_var = tk.BooleanVar(value=True)
        self.events_label = tk.StringVar(value="Events")
        return ttk.Checkbutton(parent, textvariable=self.events_label, variable=self.events_var,
                               command=self._toggle_events)

    def _build_events(self, panes):
        self.panes = panes
        self.log_frame = log_frame = ttk.Frame(panes)
        ttk.Label(log_frame, text="Events").pack(anchor="w")
        self.log = ScrolledText(log_frame, height=8, font=("Consolas", 9), wrap="none",
                                background=SURFACE, foreground=INK, relief="flat")
        self.log.pack(fill="both", expand=True)
        for level, color in LOG_COLORS.items():
            self.log.tag_configure(level, foreground=color)
        self.log.configure(state="disabled")
        panes.add(log_frame, weight=1)

    def _add_log(self, level, text):
        self.log.configure(state="normal")
        self.log.insert("end", "%s  %-7s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                 level, text), level)
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")
        if not self.events_var.get():
            self.unseen += 1
            self.events_label.set("Events (%d new)" % self.unseen)

    def _toggle_events(self):
        # Hiding only takes the pane off screen; events are still recorded.
        if self.events_var.get():
            self.panes.add(self.log_frame, weight=1)
            self.log.see("end")
            self.unseen = 0
            self.events_label.set("Events")
        else:
            self.panes.forget(self.log_frame)

    # charts ---------------------------------------------------------
    @staticmethod
    def _xlim(first, t0, end):
        """X limits that fill the window as data arrives rather than showing
        a mostly empty 6 h axis for the first few minutes."""
        left = max(t0, first) if t0 is not None else first
        return datetime.fromtimestamp(left - 60), datetime.fromtimestamp(end + 30)

    @staticmethod
    def _line(ax, pts, gap, color=SERIES, ls="-"):
        # Break the line across missing values and across gaps longer than
        # `gap` seconds, so it never bridges missing data.
        xs, ys, prev = [], [], None
        for t, v in pts:
            if prev is not None and t - prev > gap:
                xs.append(datetime.fromtimestamp(prev + 1))
                ys.append(math.nan)
            xs.append(datetime.fromtimestamp(t))
            ys.append(math.nan if v is None else v)
            prev = t
        ax.plot(xs, ys, color=color, ls=ls, lw=2, solid_capstyle="round", zorder=3)

    @staticmethod
    def _latest(ax, valid, fontsize, color=SERIES):
        """Mark the newest value of a time-ordered [(epoch, value)], and label
        it unless fontsize is None."""
        if not valid:
            return
        e, v = valid[-1]
        ax.plot([datetime.fromtimestamp(e)], [v], marker="o", markersize=8 if fontsize else 6,
                color=color, markeredgecolor=SURFACE, markeredgewidth=2 if fontsize else 1.5, zorder=5)
        if not fontsize:
            return
        ax.annotate("%.2f" % v, (datetime.fromtimestamp(e), v), xytext=(-8, 8),
                    textcoords="offset points", ha="right", color=INK, fontsize=fontsize,
                    fontweight="bold", bbox=dict(boxstyle="round,pad=0.2", fc=SURFACE,
                                                 ec="none", alpha=0.85))

    @staticmethod
    def _zero_based(ax, values):
        """Fix axis: include zero, so steady readings look steady rather than
        small noise filling the whole height. Extends below zero if needed."""
        ax.set_ylim(min(0, min(values) * 1.1), max(max(values) * 1.1, 1e-9))

    @staticmethod
    def _waiting(ax, text="waiting for readings"):
        """An empty chart: just the message, no meaningless axes. (Ticks are
        hidden rather than removed: ConciseDateFormatter fails on none.)"""
        ax.grid(False)
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        ax.text(0.5, 0.5, text, transform=ax.transAxes,
                ha="center", va="center", color=MUTED, fontsize=9)

    @staticmethod
    def _style(ax, title):
        ax.set_facecolor(SURFACE)
        ax.set_title(title, loc="left", fontsize=10, color=INK)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        # ax.clear() keeps tick settings, so undo _waiting's hiding explicitly.
        ax.tick_params(colors=MUTED, labelsize=8, length=3, left=True, bottom=True,
                       labelleft=True, labelbottom=True)
        ax.grid(axis="y", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
        # Headroom so the newest-value label never runs into the title.
        ax.margins(y=0.15)
        loc = mdates.AutoDateLocator(minticks=3)
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))

    def _set_hover(self, ax, unit, pts, missing="no reading"):
        """pts: [(epoch, value or None, tracking or a note)]; `missing` describes None."""
        self.hover[ax] = (unit, missing, [(mdates.date2num(datetime.fromtimestamp(e)), e, v, trk)
                                          for e, v, trk in pts])

    def _set_hover_shared(self, ax, series):
        """A chart with several lines. series: [(name, unit, pts)], pts as for
        _set_hover; the tooltip lists every line's value at the time pointed at."""
        self.hover[ax] = {"shared": [
            (name, unit, [(mdates.date2num(datetime.fromtimestamp(e)), e, v, n) for e, v, n in pts])
            for name, unit, pts in series]}

    @staticmethod
    def _hover_one(data, xdata):
        unit, missing, pts = data
        if not pts:
            return None
        x, t, v, trk = min(pts, key=lambda p: abs(p[0] - xdata))
        text = "%s\n%s" % (datetime.fromtimestamp(t).strftime("%d %b %H:%M:%S"),
                           missing if v is None else "%.2f %s" % (v, unit))
        if trk is False:
            text += "\nprovisional"
        elif isinstance(trk, str):                  # a note about this point
            text += "\n" + trk
        return x, v, text

    @staticmethod
    def _hover_shared(series, xdata):
        points = [p for _, _, pts in series for p in pts if p[2] is not None]
        if not points:
            return None
        x, t, v, _ = min(points, key=lambda p: abs(p[0] - xdata))
        lines = [datetime.fromtimestamp(t).strftime("%d %b %H:%M:%S")]
        for name, unit, pts in series:
            near = min(pts, key=lambda p: abs(p[1] - t), default=None)
            if near is None or abs(near[1] - t) > HOVER_MATCH_S:
                value = "-"
            else:
                value = "unknown" if near[2] is None else "%.2f %s" % (near[2], unit)
            lines.append("%s: %s" % (name, value))
        return x, v, "\n".join(lines)

    def _on_hover(self, event):
        if self.tip is not None:
            # The tip may be on the other canvas, so redraw the one it is on.
            self.tip.figure.canvas.draw_idle()
            self.tip.remove()
            self.tip = None
        data = self.hover.get(event.inaxes)
        found = None
        if data and event.xdata is not None:
            found = (self._hover_shared(data["shared"], event.xdata) if isinstance(data, dict)
                     else self._hover_one(data, event.xdata))
        if found:
            x, v, text = found
            ax = event.inaxes
            y = v if v is not None else ax.get_ylim()[0]
            left = event.x > ax.bbox.x0 + ax.bbox.width * 0.6
            self.tip = ax.annotate(
                text, (x, y), xytext=(-10 if left else 10, 10), textcoords="offset points",
                ha="right" if left else "left", fontsize=8, color=INK, zorder=10,
                bbox=dict(boxstyle="round,pad=0.4", fc="white", ec=AXIS),
                arrowprops=dict(arrowstyle="-", color=MUTED))
        event.canvas.draw_idle()
