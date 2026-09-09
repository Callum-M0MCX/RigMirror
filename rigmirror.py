"""RigMirror v0.3.003 - live antenna-routing restoration hotfix."""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from serial.tools import list_ports

from alignment import (
    ALIGNMENT_STEP_HZ,
    MAX_ALIGNMENT_HZ,
    aligned_sub_frequency,
    alignment_profile_signature,
    band_for_frequency,
    normalise_offsets,
)
from cat_engine import CATEngine, SerialSettings
from driver_runtime import (
    driver_fingerprint,
    driver_label,
    driver_local_status,
    load_driver,
    test_driver_on_engine,
)
from mirror import MirrorCoordinator
from reporting import write_test_report
from radio import (
    MAX_FREQUENCY_HZ,
    MIN_FREQUENCY_HZ,
    RadioEndpoint,
    Receiver,
    is_dual_receiver_radio,
    detect_radio,
    directional_step,
    format_frequency,
)


APP_TITLE = "RigMirror v0.3.003"
SPLIT_OFFSET_HZ = 5_000
MIRROR_FREQUENCY_CEILING_HZ = 30_000_000
DEFAULT_LAYOUT = "two"
DEFAULT_BAUD = 57600
SUPPORTED_BAUDS = (115200, 57600, 38400, 19200, 9600, 4800)
BG = "#20252b"
CARD = "#2b3138"
FIELD = "#111519"
TEXT = "#edf0f2"
MUTED = "#a4abb3"
CYAN = "#42b9e8"
GREEN = "#39c878"
RED = "#ef5a5a"
AMBER = "#f2ad3b"

DISPLAY_SCHEMES = {
    "green_black": {
        "master": ("#39c878", "#111519"),
        "listener": ("#6fa98a", "#111519"),
    },
    "cyan_black": {
        "master": ("#53d7f2", "#111519"),
        "listener": ("#73a8b2", "#111519"),
    },
    "amber_black": {
        "master": ("#ffc857", "#111519"),
        "listener": ("#b89b63", "#111519"),
    },
    "green_backlit": {
        "master": ("#111810", "#d9f2c7"),
        "listener": ("#293027", "#c2d5b5"),
    },
    "amber_backlit": {
        # The Master uses the same stronger amber as the active M button;
        # Sub retains the original, softer amber backlight for separation.
        "master": ("#17120a", "#f2ad3b"),
        "listener": ("#181409", "#f4d98b"),
    },
    "cyan_backlit": {
        "master": ("#0d1719", "#c7eef2"),
        "listener": ("#283335", "#b3d2d5"),
    },
    "white_black": {
        "master": ("#ffffff", "#111519"),
        "listener": ("#c8ccd0", "#111519"),
    },
}
DISPLAY_SCHEME_ORDER = tuple(DISPLAY_SCHEMES)
DISPLAY_SCHEME_LABELS = {
    "Green on black": "green_black",
    "Cyan on black": "cyan_black",
    "Amber on black": "amber_black",
    "Pale green backlight": "green_backlit",
    "Amber backlight": "amber_backlit",
    "Pale cyan backlight": "cyan_backlit",
    "White on black": "white_black",
}
DISPLAY_SCHEME_NAMES = {value: key for key, value in DISPLAY_SCHEME_LABELS.items()}


def application_directory() -> Path:
    """Return the folder containing RigMirror.exe, or the source files."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_directory() -> Path:
    """Return a per-user, writable folder for settings and reports."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "RigMirror"
    return Path.home() / ".config" / "RigMirror"

# These are the Kenwood antenna-memory frequency regions.  They give M a
# radio-independent way to recognise a real band change without mistaking a
# normal RIT-sized dial movement for one.
TUNING_BAND_EDGES_HZ = (
    522_000, 2_500_000, 4_100_000, 6_900_000, 7_500_000, 10_500_000,
    14_500_000, 18_500_000, 21_500_000, 25_500_000, 30_000_000,
)


def tuning_band(frequency_hz: int) -> int:
    for index, upper_edge in enumerate(TUNING_BAND_EDGES_HZ):
        if frequency_hz < upper_edge:
            return index
    return len(TUNING_BAND_EDGES_HZ)


def natural_port_key(device: str):
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", device)
    )


class RigMirrorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x420")
        self.minsize(800, 400)
        self.configure(bg=BG)

        self._application_dir = application_directory()
        self._data_dir = user_data_directory()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self._data_dir / "rigmirror_config.json"
        self._busy = [False, False]
        self._connected = [False, False]
        self._failed = [False, False]
        self._mirror_active = False
        self._quick_start_pending = False
        self._diagnostics_visible = False
        self._diagnostics_window = None
        self._diagnostics_paused = False
        self._diagnostics_lines = deque(maxlen=5000)
        self._diagnostics_autoscroll = None
        self._frequency_hz: list[Optional[int]] = [None, None]
        self._wheel_frequency: Optional[int] = None
        self._wheel_request_pending: Optional[int] = None
        self._memory_armed = False
        self._home_frequency: Optional[int] = None
        self._listen_frequency: Optional[int] = None
        self._transmitting = False
        self._pre_memory_step: Optional[int] = None
        self._split_active = False
        self._split_frequency: Optional[int] = None
        self._routing_warning = ""
        self._out_of_range = False
        self._skin_preference = "full"
        self._display_scheme = "green_black"
        self._alignment_profiles: dict[str, dict[str, int]] = {}
        self._alignment_signature: Optional[str] = None
        self._alignment_snapshot: Optional[dict[str, int]] = None
        self._alignment_preview: Optional[dict[str, int]] = None
        self._driver_test_history: dict[str, dict] = {}
        self._closing = False
        self._ui_events: queue.SimpleQueue = queue.SimpleQueue()
        self._ui_pump_id = None
        self.coordinator: Optional[MirrorCoordinator] = None

        self.layout_var = tk.StringVar(value=DEFAULT_LAYOUT)
        self.master_var = tk.IntVar(value=0)  # Master is permanently Radio 1.
        self.step_var = tk.IntVar(value=1000)
        self.tx_source_var = tk.StringVar(value="NO CHANGE")
        self.tx_action_var = tk.StringVar(value="NO CHANGE")
        self.parking_frequency_var = tk.StringVar(value="10.100.000")
        self.polling_var = tk.StringVar(value="NORMAL • 100 ms")
        self.safety_ack_var = tk.BooleanVar(value=False)
        self.port_vars = [tk.StringVar(value=""), tk.StringVar(value="")]
        self.civ_vars = [tk.StringVar(value=""), tk.StringVar(value="")]
        self.baud_vars = [
            tk.StringVar(value=str(DEFAULT_BAUD)),
            tk.StringVar(value=str(DEFAULT_BAUD)),
        ]
        self.receiver_vars = [tk.StringVar(value=Receiver.MAIN.value), tk.StringVar(value=Receiver.SUB.value)]
        self.status_vars = [tk.StringVar(value="DISCONNECTED"), tk.StringVar(value="SHARED CONNECTION")]
        self.detail_vars = [tk.StringVar(value="Select a COM port and connect."), tk.StringVar(value="Uses Radio 1 COM connection.")]
        self.model_vars = [tk.StringVar(value="AWAITING CONNECTION"), tk.StringVar(value="AWAITING CONNECTION")]
        self.role_vars = [tk.StringVar(value="MASTER"), tk.StringVar(value="SUB")]
        self.freq_vars = [tk.StringVar(value=""), tk.StringVar(value="")]
        self.alignment_band_var = tk.StringVar(value="OUTSIDE SUPPORTED AMATEUR BANDS")
        self.alignment_offset_var = tk.StringVar(value="SUB = MASTER +0 Hz")

        self.engines = [
            CATEngine(
                traffic_callback=lambda d, c, i=0: self._traffic_from_thread(i, d, c),
                information_callback=lambda c, i=0: self._information_from_thread(i, c),
            ),
            CATEngine(
                traffic_callback=lambda d, c, i=1: self._traffic_from_thread(i, d, c),
                information_callback=lambda c, i=1: self._information_from_thread(i, c),
            ),
        ]
        self.radios = [None, None]
        self._driver_paths: list[Optional[Path]] = [None, None]
        self._driver_data = [None, None]
        self.driver_name_vars = [
            tk.StringVar(value="DEFAULT RADIO • KENWOOD TS-590SG"),
            tk.StringVar(value="DEFAULT RADIO • KENWOOD TS-590SG"),
        ]
        self.driver_status_vars = [
            tk.StringVar(value="No outboard driver selected"),
            tk.StringVar(value="No outboard driver selected"),
        ]

        self._configure_styles()
        self._build_ui()
        self._load_config()
        self._refresh_ports()
        self._layout_changed(initial=True)
        self._update_roles()
        self._set_skin("full", persist=False)
        self._ui_pump_id = self.after(15, self._drain_ui_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("App.TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 18, "bold"))
        style.configure("Role.TLabel", background=CARD, foreground=CYAN, font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 9), padding=(9, 5))
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(10, 6))
        style.configure("TRadiobutton", background=BG, foreground=TEXT)
        style.configure("Card.TRadiobutton", background=CARD, foreground=TEXT)
        style.map("Card.TRadiobutton", background=[("active", CARD), ("pressed", CARD), ("focus", CARD)], foreground=[("active", TEXT)])
        style.configure("App.TCheckbutton", background=BG, foreground=TEXT)
        style.map("App.TCheckbutton", background=[("active", BG), ("pressed", BG)], foreground=[("active", TEXT)])
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT)
        style.map("Card.TCheckbutton", background=[("active", CARD), ("pressed", CARD)], foreground=[("active", TEXT)])

    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="App.TFrame", padding=8)
        root.pack(fill="both", expand=True)
        self.root_frame = root

        # The normal operating surface contains only controls used on-air.
        header = ttk.Frame(root, style="App.TFrame")
        header.pack(fill="x", pady=(0, 8))
        self.header_frame = header
        ttk.Label(header, text="RigMirror", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="v0.3.003", style="Muted.TLabel").pack(side="left", padx=10, pady=(6, 0))
        actions = ttk.Frame(header, style="App.TFrame")
        actions.pack(side="right")
        ttk.Button(actions, text="MICRO", command=lambda: self._set_skin("micro")).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="CONFIG", command=self._show_config).pack(side="left", padx=(0, 5))
        self.mirror_lamp = tk.Canvas(actions, width=22, height=22, bg=BG, highlightthickness=0)
        self.mirror_lamp.pack(side="left", padx=(0, 3))
        self.mirror_lamp_dot = self.mirror_lamp.create_oval(4, 4, 18, 18, fill=RED, outline="#7f1d1d", width=2)
        self.mirror_button = ttk.Button(actions, text="START MIRROR", style="Accent.TButton", command=self._toggle_mirror, state="normal", width=16)
        self.mirror_button.pack(side="left")

        body = ttk.Frame(root, style="App.TFrame")
        body.pack(fill="x")
        self.body_frame = body
        body.columnconfigure(0, weight=1, uniform="radio")
        body.columnconfigure(1, weight=1, uniform="radio")
        self.cards = [self._radio_card(body, 0, 0), self._radio_card(body, 1, 1)]

        tune = ttk.Frame(root, style="Card.TFrame", padding=(8, 6))
        tune.pack(fill="x", pady=(7, 0))
        self.tune_frame = tune
        ttk.Label(tune, text="STEP", style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        for label, value in (("1 kHz", 1000), ("500 Hz", 500), ("100 Hz", 100), ("10 Hz", 10)):
            ttk.Radiobutton(tune, text=label, variable=self.step_var, value=value, style="Card.TRadiobutton", command=self._save_config).pack(side="left", padx=3)
        self.memory_button = tk.Button(tune, text="M", width=3, command=self._toggle_memory, bg=FIELD, fg=TEXT, activebackground=FIELD, activeforeground=TEXT, relief="flat", font=("Segoe UI", 9, "bold"), state="disabled", cursor="hand2")
        self.memory_button.pack(side="left", padx=(12, 5))
        self.memory_status = ttk.Label(tune, text="TX/RX OFFSET OFF", style="Card.TLabel")
        self.memory_status.pack(side="left")
        self.split_button = tk.Button(
            tune, text="SPLIT +5", width=9, command=self._toggle_split,
            bg=FIELD, fg=TEXT, activebackground=FIELD, activeforeground=TEXT,
            relief="flat", font=("Segoe UI", 9, "bold"), state="disabled",
            cursor="hand2",
        )
        self.split_button.pack(side="left", padx=(14, 0))

        self._build_config_ui(root)
        self._build_alignment_ui(root)
        self._build_micro_ui(root)

    def _radio_card(self, parent, column: int, index: int):
        card = ttk.Frame(parent, style="Card.TFrame", padding=8)
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 3) if index == 0 else (3, 0))
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(
            top, textvariable=self.role_vars[index], style="Card.TLabel",
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left")
        tx_badge = None
        if index == 0:
            tx_badge = tk.Label(
                top, text="--", bg="#4b5563", fg="#eef1f4",
                font=("Segoe UI", 10, "bold"), padx=10, pady=2,
            )
            tx_badge.pack(side="left", padx=(12, 0))
        lamp = tk.Canvas(top, width=22, height=22, bg=CARD, highlightthickness=0)
        lamp.pack(side="right")
        lamp_dot = lamp.create_oval(4, 4, 18, 18, fill=RED, outline="#7f1d1d", width=2)

        identity = ttk.Frame(card, style="Card.TFrame")
        identity.pack(fill="x", pady=(10, 2))
        ttk.Label(identity, textvariable=self.model_vars[index], style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(side="left")

        status = ttk.Frame(card, style="Card.TFrame")
        status.pack(fill="x", pady=(3, 6))
        ttk.Label(status, textvariable=self.status_vars[index], style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        frequency_panel = tk.Frame(card, bg=FIELD)
        frequency_panel.pack(fill="x", pady=(4, 2))
        frequency = tk.Label(frequency_panel, textvariable=self.freq_vars[index], bg=FIELD, fg=GREEN if index == 0 else "#9ed8bb", font=("Consolas", 24, "bold"), padx=4, pady=8, cursor="sb_v_double_arrow" if index == 0 else "arrow")
        frequency.pack(side="left", fill="x", expand=True)
        range_badge = None
        if index == 1:
            range_badge = tk.Label(
                frequency_panel, text="", bg=FIELD, fg=RED,
                font=("Segoe UI", 17, "bold"), padx=5,
            )
            range_badge.pack(side="right")
        frequency.bind("<MouseWheel>", lambda event, i=index: self._mouse_wheel(event, i))
        frequency.bind("<Button-4>", lambda _event, i=index: self._tune_from_wheel(1, i))
        frequency.bind("<Button-5>", lambda _event, i=index: self._tune_from_wheel(-1, i))
        frequency.bind("<Button-3>", self._cycle_display_scheme)

        return {
            "frame": card, "lamp": lamp, "lamp_dot": lamp_dot,
            "frequency": frequency, "identity": identity, "tx_badge": tx_badge,
            "range_badge": range_badge,
        }

    def _build_config_ui(self, parent) -> None:
        config = ttk.Frame(parent, style="App.TFrame")
        self.config_frame = config

        header = ttk.Frame(config, style="App.TFrame")
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="CONFIG", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Connections and operating options", style="Muted.TLabel").pack(side="left", padx=12, pady=(6, 0))
        buttons = ttk.Frame(header, style="App.TFrame")
        buttons.pack(side="right")
        self.alignment_button = ttk.Button(
            buttons, text="SUB ALIGNMENT…", command=self._show_alignment,
        )
        self.alignment_button.pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="DIAGNOSTICS", command=self._show_diagnostics).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="CANCEL", command=self._cancel_config).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="APPLY", style="Accent.TButton", command=self._apply_config).pack(side="left")

        layout = ttk.Frame(config, style="Card.TFrame", padding=(10, 8))
        layout.pack(fill="x", pady=(0, 7))
        self.layout_frame = layout
        ttk.Label(layout, text="RADIO LAYOUT", style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 12))
        same_button = ttk.Radiobutton(layout, text="Same radio / dual receiver", variable=self.layout_var, value="same", style="Card.TRadiobutton", command=self._layout_changed)
        same_button.pack(side="left", padx=6)
        two_button = ttk.Radiobutton(layout, text="Two physical radios", variable=self.layout_var, value="two", style="Card.TRadiobutton", command=self._layout_changed)
        two_button.pack(side="left", padx=6)
        self.layout_buttons = (same_button, two_button)
        self.layout_note = ttk.Label(layout, text="", style="Card.TLabel")
        self.layout_note.pack(side="right")

        connections = ttk.Frame(config, style="App.TFrame")
        connections.pack(fill="x")
        connections.columnconfigure(0, weight=1, uniform="connection")
        connections.columnconfigure(1, weight=1, uniform="connection")
        for index in (0, 1):
            controls = self._config_radio_card(connections, index, index)
            self.cards[index].update(controls)

        options = ttk.Frame(config, style="App.TFrame")
        options.pack(fill="x", pady=(7, 0))
        options.columnconfigure(0, weight=1, uniform="options")
        options.columnconfigure(1, weight=1, uniform="options")

        display = ttk.Frame(options, style="Card.TFrame", padding=10)
        display.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        self.display_frame = display
        ttk.Label(display, text="FREQUENCY DISPLAY", style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(display, text="Right-clicking a readout still cycles the schemes.", style="Card.TLabel", wraplength=400).pack(anchor="w", pady=(5, 8))
        self.config_display_var = tk.StringVar(value="Green on black")
        ttk.Combobox(
            display, textvariable=self.config_display_var,
            values=tuple(DISPLAY_SCHEME_LABELS), state="readonly", width=25,
        ).pack(anchor="w")
        poll_row = ttk.Frame(display, style="Card.TFrame")
        poll_row.pack(fill="x", pady=(10, 0))
        ttk.Label(poll_row, text="CAT POLLING", style="Card.TLabel").pack(side="left")
        self.config_polling_var = tk.StringVar(value="NORMAL • 100 ms")
        ttk.Combobox(
            poll_row, textvariable=self.config_polling_var,
            values=("NORMAL • 100 ms", "FAST • 50 ms"), state="readonly", width=19,
        ).pack(side="left", padx=(10, 0))
        ttk.Label(
            display, text="Fast may overload slower or older CAT interfaces.",
            style="Card.TLabel", wraplength=390,
        ).pack(anchor="w", pady=(4, 0))

        routing = ttk.Frame(options, style="Card.TFrame", padding=10)
        routing.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        self.routing_frame = routing
        ttk.Label(routing, text="SUB RF ROUTING", style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(
            routing,
            text=("CAT diversion begins only after Master TX is detected. It reduces continuing "
                  "RF exposure but is not guaranteed equipment protection."),
            style="Card.TLabel", wraplength=400, justify="left",
        ).pack(anchor="w", pady=(5, 8))
        route_row = ttk.Frame(routing, style="Card.TFrame")
        route_row.pack(fill="x")
        ttk.Label(route_row, text="ON MASTER TX", style="Card.TLabel").pack(side="left")
        self.config_action_var = tk.StringVar(value="NO CHANGE")
        self.config_action_combo = ttk.Combobox(
            route_row, textvariable=self.config_action_var,
            values=("NO CHANGE", "SWITCH ANTENNA / RX PORT", "PARKING FREQUENCY"),
            state="readonly", width=28,
        )
        self.config_action_combo.pack(side="left", padx=(10, 0))
        self.config_action_combo.bind("<<ComboboxSelected>>", lambda _event: self._protection_action_changed())

        destination_row = ttk.Frame(routing, style="Card.TFrame")
        destination_row.pack(fill="x", pady=(6, 0))
        self.config_destination_label = ttk.Label(destination_row, text="DESTINATION", style="Card.TLabel")
        self.config_destination_label.pack(side="left")
        self.config_tx_var = tk.StringVar(value="ANT2")
        self.config_tx_combo = ttk.Combobox(
            destination_row, textvariable=self.config_tx_var,
            values=("ANT1", "ANT2", "RX ANT"), state="readonly", width=16,
        )
        self.config_tx_combo.pack(side="left", padx=(22, 0))
        self.config_parking_label = ttk.Label(destination_row, text="PARK AT", style="Card.TLabel")
        self.config_parking_entry = ttk.Entry(
            destination_row, textvariable=self.parking_frequency_var, width=14,
        )
        self.config_parking_units = ttk.Label(destination_row, text="MHz format", style="Card.TLabel")
        self.config_safety_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            routing,
            text="I understand that CAT switching is best-effort routing, not RF protection.",
            variable=self.config_safety_var, style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(9, 0))
        self._protection_action_changed()

    def _build_alignment_ui(self, parent) -> None:
        frame = ttk.Frame(parent, style="App.TFrame")
        self.alignment_frame = frame

        header = ttk.Frame(frame, style="App.TFrame")
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="SUB VFO ALIGNMENT", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header, text="Per-band correction for this Master/Sub pairing",
            style="Muted.TLabel",
        ).pack(side="left", padx=12, pady=(6, 0))

        body = ttk.Frame(frame, style="Card.TFrame", padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Select the required band on the MASTER radio before adjusting alignment.",
            style="Card.TLabel", justify="center",
        ).pack(anchor="center", pady=(2, 8))
        ttk.Label(
            body, textvariable=self.alignment_band_var, style="Card.TLabel",
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="center", pady=(0, 12))

        frequencies = ttk.Frame(body, style="Card.TFrame")
        frequencies.pack(fill="x", pady=(0, 14))
        frequencies.columnconfigure(0, weight=1, uniform="alignment-frequency")
        frequencies.columnconfigure(1, weight=1, uniform="alignment-frequency")
        for column, title in ((0, "MASTER"), (1, "SUB")):
            card = ttk.Frame(frequencies, style="Card.TFrame", padding=6)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0))
            ttk.Label(card, text=title, style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack()
            tk.Label(
                card, textvariable=self.freq_vars[column], bg=FIELD,
                fg=GREEN if column == 0 else "#9ed8bb",
                font=("Consolas", 22, "bold"), padx=10, pady=8,
            ).pack(fill="x", pady=(5, 0))

        ttk.Label(
            body, textvariable=self.alignment_offset_var, style="Card.TLabel",
            font=("Consolas", 16, "bold"),
        ).pack(pady=(0, 12))

        adjust = ttk.Frame(body, style="Card.TFrame")
        adjust.pack()
        ttk.Button(
            adjust, text=f"−{ALIGNMENT_STEP_HZ} Hz", width=14,
            command=lambda: self._adjust_alignment(-ALIGNMENT_STEP_HZ),
        ).pack(side="left", padx=5)
        ttk.Button(
            adjust, text=f"+{ALIGNMENT_STEP_HZ} Hz", width=14,
            command=lambda: self._adjust_alignment(ALIGNMENT_STEP_HZ),
        ).pack(side="left", padx=5)

        ttk.Label(
            body,
            text=("Listen to Master and Sub together, adjust for the closest audible alignment, "
                  "then press SET."),
            style="Card.TLabel", justify="center", wraplength=650,
        ).pack(pady=(14, 0))

        buttons = ttk.Frame(frame, style="App.TFrame")
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="RESET ALIGNMENT", command=self._reset_alignment).pack(side="left")
        ttk.Button(buttons, text="RESET ALL", command=self._reset_all_alignments).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="CANCEL", command=self._cancel_alignment).pack(side="right", padx=(6, 0))
        ttk.Button(
            buttons, text="SET", style="Accent.TButton", command=self._set_alignment,
        ).pack(side="right")

    def _config_radio_card(self, parent, column: int, index: int):
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 3) if index == 0 else (3, 0))
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(
            top, text="MASTER CONNECTION" if index == 0 else "SUB CONNECTION",
            style="Card.TLabel", font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        config_lamp = tk.Canvas(top, width=22, height=22, bg=CARD, highlightthickness=0)
        config_lamp.pack(side="right")
        config_lamp_dot = config_lamp.create_oval(
            4, 4, 18, 18, fill=RED, outline="#7f1d1d", width=2,
        )

        driver_box = ttk.Frame(card, style="Card.TFrame")
        driver_box.pack(fill="x", pady=(7, 1))
        ttk.Label(
            driver_box, textvariable=self.driver_name_vars[index],
            style="Card.TLabel", font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            driver_box, textvariable=self.driver_status_vars[index],
            style="Card.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(1, 4))
        load_driver_button = ttk.Button(
            driver_box,
            text="LOAD MASTER RADIO…" if index == 0 else "LOAD SUB / SLAVE RADIO…",
            command=lambda i=index: self._choose_driver(i),
        )
        load_driver_button.grid(row=2, column=0, sticky="w")
        test_driver_button = ttk.Button(
            driver_box, text="TEST DRIVER", command=lambda i=index: self._test_driver(i),
            state="disabled",
        )
        test_driver_button.grid(row=2, column=1, sticky="w", padx=(6, 0))
        built_in_button = ttk.Button(
            driver_box, text="USE DEFAULT 590", command=lambda i=index: self._clear_driver(i),
        )
        built_in_button.grid(row=2, column=2, sticky="w", padx=(6, 0))
        civ_label = ttk.Label(driver_box, text="CI-V address (hex)", style="Card.TLabel")
        civ_label.grid(row=3, column=0, sticky="w", pady=(5, 0))
        civ_entry = ttk.Entry(driver_box, textvariable=self.civ_vars[index], width=8)
        civ_entry.grid(row=3, column=1, sticky="w", padx=(6, 0))
        civ_label.grid_remove()
        civ_entry.grid_remove()

        comms_slot = ttk.Frame(card, style="Card.TFrame", height=62)
        comms_slot.pack(fill="x", pady=(7, 0))
        comms_slot.pack_propagate(False)
        comms = ttk.Frame(comms_slot, style="Card.TFrame")
        comms.pack(fill="x")
        ttk.Label(comms, text="COM", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(comms, text="BAUD", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        port = ttk.Combobox(comms, textvariable=self.port_vars[index], width=16)
        port.grid(row=1, column=0, sticky="ew")
        baud = ttk.Combobox(
            comms, textvariable=self.baud_vars[index],
            values=[str(value) for value in SUPPORTED_BAUDS], state="readonly", width=9,
        )
        baud.grid(row=1, column=1, padx=(8, 0))
        refresh = ttk.Button(comms, text="REFRESH", width=9, command=self._refresh_ports)
        refresh.grid(row=1, column=2, padx=(8, 0))
        connect = ttk.Button(comms, text="CONNECT", width=10, command=lambda i=index: self._connect(i))
        connect.grid(row=1, column=3, padx=(6, 0))
        shared_comms = ttk.Frame(comms_slot, style="Card.TFrame")
        ttk.Label(shared_comms, text="SHARED COM CONNECTION", style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 3))
        ttk.Label(shared_comms, textvariable=self.detail_vars[index], style="Card.TLabel").pack(anchor="w")

        status = ttk.Frame(card, style="Card.TFrame")
        status.pack(fill="x", pady=(5, 0))
        ttk.Label(status, textvariable=self.model_vars[index], style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(status, textvariable=self.status_vars[index], style="Card.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(status, textvariable=self.detail_vars[index], style="Card.TLabel", wraplength=410).pack(anchor="w", pady=(2, 0))
        return {
            "config_frame": card, "port": port, "baud": baud,
            "refresh": refresh, "connect": connect, "comms_slot": comms_slot,
            "comms": comms, "shared_comms": shared_comms,
            "config_lamp": config_lamp, "config_lamp_dot": config_lamp_dot,
            "load_driver": load_driver_button, "test_driver": test_driver_button,
            "built_in_driver": built_in_button,
            "civ_label": civ_label, "civ_entry": civ_entry,
        }

    def _build_micro_ui(self, parent) -> None:
        micro = ttk.Frame(parent, style="Card.TFrame", padding=(3, 4))
        self.micro_frame = micro

        upper = ttk.Frame(micro, style="Card.TFrame")
        upper.pack(fill="x")
        ttk.Label(upper, text="RigMirror", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.micro_tx_badge = tk.Label(
            upper, text="--", bg="#4b5563", fg="#eef1f4",
            font=("Segoe UI", 9, "bold"), padx=8, pady=1,
        )
        self.micro_tx_badge.pack(side="left", padx=(7, 0))
        ttk.Button(upper, text="FULL", command=lambda: self._set_skin("full")).pack(side="right")

        readouts = ttk.Frame(micro, style="Card.TFrame")
        readouts.pack(fill="x", pady=(4, 4))
        readouts.columnconfigure(0, weight=1)
        readouts.columnconfigure(2, weight=1)
        master_frequency = tk.Label(
            readouts, textvariable=self.freq_vars[0], bg=FIELD, fg=GREEN,
            font=("Consolas", 18, "bold"), width=12, padx=2, pady=2,
            cursor="sb_v_double_arrow",
        )
        master_frequency.grid(row=0, column=0, sticky="w")
        self.micro_range_badge = tk.Label(
            readouts, text="", bg=CARD, fg=RED,
            font=("Segoe UI", 9, "bold"), padx=4,
        )
        self.micro_range_badge.grid(row=0, column=1, padx=3)
        listener_frequency = tk.Label(
            readouts, textvariable=self.freq_vars[1], bg=FIELD, fg="#9ed8bb",
            font=("Consolas", 18, "bold"), width=12, padx=2, pady=2,
        )
        listener_frequency.grid(row=0, column=2, sticky="e")
        self.micro_frequencies = (master_frequency, listener_frequency)
        master_frequency.bind("<MouseWheel>", lambda event: self._mouse_wheel(event, 0))
        master_frequency.bind("<Button-4>", lambda _event: self._tune_from_wheel(1, 0))
        master_frequency.bind("<Button-5>", lambda _event: self._tune_from_wheel(-1, 0))
        master_frequency.bind("<Button-3>", self._cycle_display_scheme)
        listener_frequency.bind("<Button-3>", self._cycle_display_scheme)

        controls = ttk.Frame(micro, style="Card.TFrame")
        controls.pack(fill="x")
        ttk.Label(controls, text="STEP", style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 5))
        for label, value in (("1k", 1000), ("500", 500), ("100", 100), ("10", 10)):
            ttk.Radiobutton(
                controls, text=label, variable=self.step_var, value=value,
                style="Card.TRadiobutton", command=self._save_config,
            ).pack(side="left", padx=3)
        self.micro_memory_button = tk.Button(
            controls, text="M", width=3, command=self._toggle_memory,
            bg=FIELD, fg=TEXT, activebackground=FIELD, activeforeground=TEXT,
            relief="flat", font=("Segoe UI", 9, "bold"), state="disabled", cursor="hand2",
        )
        self.micro_memory_button.pack(side="left", padx=(9, 5))
        self.micro_memory_status = ttk.Label(controls, text="OFFSET OFF", style="Card.TLabel")
        self.micro_memory_status.pack(side="left")
        self.micro_split_button = tk.Button(
            controls, text="SPLIT +5", width=8, command=self._toggle_split,
            bg=FIELD, fg=TEXT, activebackground=FIELD, activeforeground=TEXT,
            relief="flat", font=("Segoe UI", 9, "bold"), state="disabled",
            cursor="hand2",
        )
        self.micro_split_button.pack(side="right", padx=(6, 0))

    def _layout_changed(self, initial: bool = False) -> None:
        if not initial and (any(self._connected) or any(self._busy)):
            messagebox.showinfo("Disconnect First", "Disconnect before changing radio layout.", parent=self)
            self.layout_var.set("same" if self._connected[1] and self.engines[0].is_open and not self.engines[1].is_open else "two")
            return
        same = self.layout_var.get() == "same"
        if same:
            self.receiver_vars[0].set(Receiver.MAIN.value)
            self.receiver_vars[1].set(Receiver.SUB.value)
        else:
            self.receiver_vars[0].set(Receiver.MAIN.value)
            self.receiver_vars[1].set(Receiver.MAIN.value)
        if same:
            self.cards[1]["connect"].configure(text="SHARED")
            self.cards[1]["comms"].pack_forget()
            self.cards[1]["shared_comms"].pack(fill="x")
            self.status_vars[1].set("SHARED CONNECTION")
            self.detail_vars[1].set("Uses Radio 1 COM connection.")
            self.layout_note.configure(text="Main + Sub receivers over one COM port")
            self.routing_frame.grid_remove()
            self.display_frame.grid_configure(column=0, columnspan=2, padx=0)
        else:
            self.cards[1]["shared_comms"].pack_forget()
            self.cards[1]["comms"].pack(fill="x", pady=(2, 0))
            self.cards[1]["connect"].configure(text="CONNECT")
            self.status_vars[1].set("DISCONNECTED")
            self.detail_vars[1].set("Select its own COM port and connect.")
            self.layout_note.configure(text="Independent COM connection for each radio")
            self.routing_frame.grid()
            self.routing_frame.grid_configure(column=1, columnspan=1, padx=(3, 0))
            self.display_frame.grid_configure(column=0, columnspan=1, padx=(0, 3))
        self._refresh_routing_choices()
        self._set_connection_controls(0, not self._busy[0])
        self._set_connection_controls(1, not self._busy[1])
        self._update_config_lock_state()
        self._save_config()

    def _choose_driver(self, index: int) -> None:
        if self._connected[index] or self._busy[index]:
            messagebox.showinfo(
                "Disconnect First", "Disconnect this radio before changing its driver.", parent=self,
            )
            return
        filename = filedialog.askopenfilename(
            parent=self,
            title="Load Master Radio" if index == 0 else "Load Sub / Slave Radio",
            initialdir=self._application_dir / "drivers",
            filetypes=(("RigMirror radio drivers", "*.rmradio"), ("All files", "*.*")),
        )
        if not filename:
            return
        try:
            path = Path(filename).resolve()
            driver = load_driver(path)
        except Exception as exc:
            messagebox.showerror("Driver Could Not Be Loaded", str(exc), parent=self)
            return
        self._driver_paths[index] = path
        self._driver_data[index] = driver
        self.civ_vars[index].set(str(driver.get("transport", {}).get("civ_address", "")))
        default_baud = driver.get("transport", {}).get("default_baud")
        if default_baud in SUPPORTED_BAUDS:
            self.baud_vars[index].set(str(default_baud))
        self._show_driver_choice(index)
        self._set_connection_controls(index, True)

    def _clear_driver(self, index: int) -> None:
        """Return one endpoint to RigMirror's proven TS-590SG fallback."""
        if self._connected[index] or self._busy[index]:
            messagebox.showinfo(
                "Disconnect First", "Disconnect this radio before changing its driver.", parent=self,
            )
            return
        self._driver_paths[index] = None
        self._driver_data[index] = None
        self._show_driver_choice(index)
        self.model_vars[index].set("AWAITING CONNECTION")
        self.status_vars[index].set("DISCONNECTED")
        self.detail_vars[index].set("Default fallback expects a Kenwood TS-590SG.")
        self._set_connection_controls(index, True)
        self._save_config()

    def _show_driver_choice(self, index: int) -> None:
        driver = self._driver_data[index]
        civ = (driver or {}).get("transport", {}).get("protocol") == "icom_civ"
        for key in ("civ_label", "civ_entry"):
            widget = self.cards[index][key]
            widget.grid() if civ else widget.grid_remove()
        if driver is None:
            self.driver_name_vars[index].set("DEFAULT RADIO • KENWOOD TS-590SG")
            self.driver_status_vars[index].set("No outboard driver selected")
            return
        self.driver_name_vars[index].set(driver_label(driver).upper())
        status = driver_local_status(self._driver_paths[index], self._driver_test_history)
        revision = driver["metadata"].get("driver_revision", "?")
        label = {
            "TESTED": "TESTED SUCCESSFULLY ON THIS PC",
            "FAILED": "TEST FAILED",
            "NOT TESTED": "NOT YET TESTED",
        }[status]
        self.driver_status_vars[index].set(f"Driver revision {revision} • {label}")

    def _serial_settings(self, index: int, port: str, baud: int) -> SerialSettings:
        """Apply serial details supplied by an outboard driver when present."""
        transport = (self._driver_data[index] or {}).get("transport", {})
        return SerialSettings(
            port=port,
            baud_rate=baud,
            timeout_seconds=float(transport.get("timeout_seconds", 0.7)),
            stop_bits=int(transport.get("stop_bits", 1)),
            protocol=transport.get("protocol", "semicolon_ascii"),
            civ_address=self._civ_address(index, transport),
            controller_address=int(str(transport.get("controller_address", "E0")), 16),
        )

    def _civ_address(self, index: int, transport) -> int:
        if transport.get("protocol") != "icom_civ":
            return 0x94
        value = int(self.civ_vars[index].get().strip() or str(transport.get("civ_address", "94")), 16)
        if not 1 <= value < 0xE0:
            raise ValueError("CI-V radio address must be hexadecimal 01 through DF.")
        return value

    def _test_driver(self, index: int) -> None:
        if self._driver_data[index] is None or self._driver_paths[index] is None:
            return
        if self._connected[index] or self._busy[index]:
            messagebox.showinfo(
                "Disconnect First", "Disconnect this radio before testing its driver.", parent=self,
            )
            return
        port = self.port_vars[index].get().strip()
        try:
            baud = int(self.baud_vars[index].get())
        except ValueError:
            messagebox.showerror("Invalid Baud Rate", "Choose a valid baud rate.", parent=self)
            return
        if not port:
            messagebox.showerror("No COM Port", "Choose the radio's COM port first.", parent=self)
            return

        driver = self._driver_data[index]
        path = self._driver_paths[index]
        try:
            settings = self._serial_settings(index, port, baud)
        except ValueError as exc:
            messagebox.showerror("Invalid Connection Settings", str(exc), parent=self)
            return
        self._busy[index] = True
        self.driver_status_vars[index].set("TESTING…")
        self._set_connection_controls(index, False)
        self._update_config_lock_state()

        def worker() -> None:
            engine = self.engines[index]
            try:
                engine.open(settings)
                time.sleep(0.35)
                results = test_driver_on_engine(driver, engine)
            except Exception as exc:
                self._post_ui(self._driver_test_finished, index, False, str(exc), None)
            else:
                self._post_ui(self._driver_test_finished, index, True, "", results)
            finally:
                engine.close()

        threading.Thread(target=worker, daemon=True).start()

    def _driver_test_finished(self, index: int, success: bool, detail: str, results) -> None:
        self._busy[index] = False
        self._set_connection_controls(index, True)
        self._update_config_lock_state()
        if not success:
            fingerprint = driver_fingerprint(self._driver_paths[index])
            self._driver_test_history[fingerprint] = {
                "status": "FAILED",
                "tested_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "driver_revision": self._driver_data[index]["metadata"].get("driver_revision"),
                "detail": detail[-500:],
            }
            self._show_driver_choice(index)
            self._save_config()
            report = self._write_failure_report(index, "driver_test", detail)
            suffix = f"\n\nFailure report saved:\n{report}" if report else ""
            messagebox.showerror("Driver Test Failed", detail + suffix, parent=self)
            return
        fingerprint = driver_fingerprint(self._driver_paths[index])
        self._driver_test_history[fingerprint] = {
            "status": "TESTED",
            "tested_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "driver_revision": self._driver_data[index]["metadata"].get("driver_revision"),
            "results": list(results or []),
        }
        self._show_driver_choice(index)
        self._save_config()
        messagebox.showinfo(
            "Driver Tested Successfully",
            "The driver passed RigMirror's commissioning tests:\n\n" + "\n".join(results),
            parent=self,
        )

    def _test_port(self, index: int) -> None:
        """Prove Windows can claim and release the port without querying the radio."""
        if self._connected[index] or self._busy[index]:
            return
        port = self.port_vars[index].get().strip()
        try:
            baud = int(self.baud_vars[index].get())
        except ValueError:
            messagebox.showerror("Invalid Baud Rate", "Choose a valid baud rate.", parent=self)
            return
        if not port:
            messagebox.showerror("No COM Port", "Choose a COM port first.", parent=self)
            return
        self._busy[index] = True
        self.status_vars[index].set("TESTING PORT")
        self.detail_vars[index].set(f"Opening and closing {port} at {baud} without sending CAT data…")
        self._set_connection_controls(index, False)

        def worker() -> None:
            engine = self.engines[index]
            try:
                engine.open(self._serial_settings(index, port, baud))
                time.sleep(0.15)
            except Exception as exc:
                self._post_ui(self._port_test_finished, index, False, str(exc))
            else:
                self._post_ui(self._port_test_finished, index, True, "")
            finally:
                engine.close()

        threading.Thread(target=worker, daemon=True).start()

    def _port_test_finished(self, index: int, success: bool, detail: str) -> None:
        self._busy[index] = False
        self._set_connection_controls(index, True)
        if success:
            port = self.port_vars[index].get().strip()
            self.status_vars[index].set("PORT TEST PASSED")
            self.detail_vars[index].set(f"{port} opened and closed cleanly; the radio was not queried.")
            messagebox.showinfo(
                "COM Port Passed",
                f"{port} opened and closed successfully.\n\nThis proves Windows port access only; it does not prove the radio link.",
                parent=self,
            )
            return
        self.status_vars[index].set("PORT TEST FAILED")
        self.detail_vars[index].set(detail)
        report = self._write_failure_report(index, "port_test", detail)
        suffix = f"\n\nFailure report saved:\n{report}" if report else ""
        messagebox.showerror("COM Port Failed", detail + suffix, parent=self)

    def _write_failure_report(self, index: int, category: str, detail: str) -> Optional[Path]:
        try:
            return write_test_report(
                self._data_dir,
                category=category,
                endpoint="MASTER" if index == 0 else "SUB / SLAVE",
                port=self.port_vars[index].get().strip(),
                baud=int(self.baud_vars[index].get()),
                outcome="FAILED",
                detail=detail,
                traffic=list(self._diagnostics_lines)[-250:],
                driver=self._driver_data[index],
                driver_filename=(self._driver_paths[index].name if self._driver_paths[index] else ""),
                connection_settings={
                    "protocol": (self._driver_data[index] or {}).get("transport", {}).get("protocol", "semicolon_ascii"),
                    "civ_address_override": self.civ_vars[index].get().strip(),
                    "driver_sha256": (
                        driver_fingerprint(self._driver_paths[index])
                        if self._driver_paths[index] else "built-in"
                    ),
                },
            )
        except Exception:
            return None

    def _stored_driver_path(self, path: Optional[Path]) -> str:
        if path is None:
            return ""
        base = self._application_dir
        try:
            return str(path.resolve().relative_to(base))
        except ValueError:
            return str(path.resolve())

    def _resolved_driver_path(self, stored: str) -> Optional[Path]:
        if not stored:
            return None
        path = Path(stored)
        if not path.is_absolute():
            path = self._application_dir / path
        return path.resolve()

    def _refresh_ports(self) -> None:
        ports = sorted((port.device for port in list_ports.comports()), key=natural_port_key)
        for index, combo in enumerate((self.cards[0]["port"], self.cards[1]["port"])):
            combo["values"] = ports
            if not self.port_vars[index].get().strip() and ports:
                used = {self.port_vars[other].get().strip() for other in (0, 1) if other != index}
                available = [port for port in ports if port not in used]
                if available:
                    self.port_vars[index].set(available[0])

    def _refresh_routing_choices(self) -> None:
        """Rebuild routing choices from the currently detected Sub model."""
        supported: tuple[str, ...] = ()
        if self.layout_var.get() == "two":
            sub_radio = self.radios[1]
            if sub_radio is None:
                supported = ("ANT1", "ANT2", "RX ANT")
            else:
                receiver = Receiver.MAIN if is_dual_receiver_radio(sub_radio) else None
                supported = RadioEndpoint(
                    sub_radio, receiver, "Sub",
                ).supported_receive_sources()

        choices = tuple(supported)
        self.config_tx_combo.configure(values=choices)
        if choices and self.config_tx_var.get() not in choices:
            self.config_tx_var.set(choices[0])
        actions = ["NO CHANGE", "PARKING FREQUENCY"]
        if choices:
            actions.insert(1, "SWITCH ANTENNA / RX PORT")
        self.config_action_combo.configure(values=tuple(actions))
        if self.config_action_var.get() not in actions:
            self.config_action_var.set("NO CHANGE")
        if self.tx_action_var.get() not in actions:
            self.tx_action_var.set("NO CHANGE")
            self.safety_ack_var.set(False)
        self._protection_action_changed()

    def _protection_action_changed(self) -> None:
        if not hasattr(self, "config_action_var"):
            return
        action = self.config_action_var.get()
        antenna = action == "SWITCH ANTENNA / RX PORT"
        parking = action == "PARKING FREQUENCY"
        self.config_destination_label.configure(foreground=TEXT if antenna else MUTED)
        self.config_tx_combo.configure(state="readonly" if antenna else "disabled")
        if parking:
            self.config_parking_label.pack(side="left")
            self.config_parking_entry.pack(side="left", padx=(10, 4))
            self.config_parking_units.pack(side="left")
        else:
            self.config_parking_label.pack_forget()
            self.config_parking_entry.pack_forget()
            self.config_parking_units.pack_forget()

    @staticmethod
    def _parse_parking_frequency(text: str) -> int:
        value = text.strip().replace(",", ".")
        if value.count(".") >= 2:
            digits = "".join(part for part in value.split(".") if part)
            if not digits.isdigit():
                raise ValueError("Parking frequency must look like 10.100.000 or 10.100 MHz.")
            frequency = int(digits)
        elif "." in value:
            frequency = round(float(value) * 1_000_000)
        else:
            frequency = int(value)
            if frequency < 1_000:
                frequency *= 1_000_000
        if not MIN_FREQUENCY_HZ <= frequency <= MIRROR_FREQUENCY_CEILING_HZ:
            raise ValueError("Parking frequency must be between 0.030 and 30.000 MHz.")
        return frequency

    def _connect(self, index: int) -> None:
        if self._busy[index]:
            return
        if self._connected[index]:
            self._disconnect_radio(index, "Disconnected by operator.")
            return
        if self.layout_var.get() == "same" and index == 1:
            return
        selected_driver = self._driver_data[index]
        selected_driver_path = self._driver_paths[index]
        port = self.port_vars[index].get().strip()
        try:
            baud = int(self.baud_vars[index].get())
        except ValueError:
            messagebox.showerror("Invalid Baud Rate", "Choose a valid baud rate.", parent=self)
            return
        if not port:
            messagebox.showerror("No COM Port", f"Choose a COM port for Radio {index + 1}.", parent=self)
            return
        same_layout = self.layout_var.get() == "same"
        try:
            settings = self._serial_settings(index, port, baud)
        except ValueError as exc:
            messagebox.showerror("Invalid Connection Settings", str(exc), parent=self)
            return
        selected_receiver = Receiver.MAIN
        if self.layout_var.get() == "two" and self._connected[1 - index] and port == self.port_vars[1 - index].get().strip():
            messagebox.showerror("COM Port In Use", "Two physical radios require different COM ports.", parent=self)
            return

        self._stop_coordinator()
        if index == 0:
            self._set_tx_indicator(None)
        self._busy[index] = True
        self._failed[index] = False
        self._set_connection_controls(index, False)
        self._set_lamp(index, AMBER)
        self.status_vars[index].set("CONNECTING")
        self.model_vars[index].set("")
        self.receiver_vars[index].set("")
        self._frequency_hz[index] = None
        self.freq_vars[index].set("")
        self.detail_vars[index].set(f"Opening {port} at {baud} and identifying radio …")

        def worker() -> None:
            try:
                engine = self.engines[index]
                engine.open(settings)
                # USB serial interfaces and the TS-990 sometimes need a short
                # settling period after Windows opens the COM port.  Retry the
                # identification handshake within the same button press.
                time.sleep(0.35)
                radio = None
                last_error = None
                for attempt in range(3):
                    try:
                        radio = detect_radio(engine, selected_driver, selected_driver_path)
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            time.sleep(0.25)
                if radio is None:
                    raise RuntimeError(f"Radio identification failed after 3 attempts: {last_error}")
                identity = radio.identity
                radio.enable_auto_information()
                if same_layout:
                    if not is_dual_receiver_radio(radio):
                        raise RuntimeError("Same radio mode requires a driver with two independently addressable receivers.")
                    values = (
                        radio.read_frequency(Receiver.MAIN),
                        radio.read_frequency(Receiver.SUB),
                    )
                    selections = (Receiver.MAIN.value, Receiver.SUB.value)
                elif is_dual_receiver_radio(radio):
                    values = (radio.read_frequency(selected_receiver),)
                    selections = (selected_receiver.value,)
                else:
                    frequency_hz, active_vfo = radio.read_frequency()
                    values = (frequency_hz,)
                    selections = (active_vfo.value,)
            except Exception as exc:
                self.engines[index].close()
                self._post_ui(self._connect_finished, index, False, str(exc), None, None, None)
                return
            self._post_ui(
                self._connect_finished, index, True,
                f"{identity.model} on {port} at {baud}.", values, radio, selections,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _connect_finished(self, index: int, success: bool, detail: str, values, radio, selections) -> None:
        self._busy[index] = False
        self._set_connection_controls(index, True)
        if not success:
            self._failed[index] = True
            self._connected[index] = False
            self.status_vars[index].set("CONNECTION FAILED")
            self.detail_vars[index].set(detail)
            self.model_vars[index].set("")
            self.receiver_vars[index].set("")
            self._frequency_hz[index] = None
            self.freq_vars[index].set("")
            self._set_lamp(index, RED)
            self.cards[index]["connect"].configure(text="RETRY")
            if index == 0:
                self._set_tx_indicator(None)
            if self.layout_var.get() == "same" and index == 0:
                self._connected[1] = False
                self.status_vars[1].set("CONNECTION FAILED")
                self.model_vars[1].set("")
                self.receiver_vars[1].set("")
                self._frequency_hz[1] = None
                self.freq_vars[1].set("")
                self._set_lamp(1, RED)
            self._set_connection_controls(index, True)
            self._update_config_lock_state()
            self._refresh_routing_choices()
            if self._quick_start_pending:
                self._quick_start_pending = False
                self.mirror_button.configure(text="START MIRROR")
                self._show_config()
            self._update_mirror_availability()
            return
        self.radios[index] = radio
        self._connected[index] = True
        self.model_vars[index].set(radio.identity.model.upper())
        self.receiver_vars[index].set(selections[0])
        self.status_vars[index].set("CONNECTED")
        self.detail_vars[index].set(detail)
        self._set_lamp(index, GREEN)
        self.cards[index]["connect"].configure(text="DISCONNECT")
        if index == 0:
            self._set_tx_indicator(False)
        if self.layout_var.get() == "same":
            self.radios[1] = radio
            self.model_vars[1].set(radio.identity.model.upper())
            self.receiver_vars[0].set(selections[0])
            self.receiver_vars[1].set(selections[1])
            self._connected[1] = True
            self.status_vars[1].set("CONNECTED • SHARED COM")
            port = self.port_vars[0].get().strip()
            baud = self.baud_vars[0].get()
            self.detail_vars[1].set(f"SHARED: {port} @ {baud}")
            self._set_lamp(1, GREEN)
            self._frequency_update(0, values[0])
            self._frequency_update(1, values[1])
        else:
            self._frequency_update(index, values[0])
        self._refresh_routing_choices()
        self._set_connection_controls(0, True)
        self._set_connection_controls(1, True)
        self._update_config_lock_state()
        self._save_config()
        self._try_start_coordinator()
        if (self._quick_start_pending and all(self._connected)
                and self.coordinator is not None):
            self._quick_start_pending = False
            self._update_mirror_availability()
            self._toggle_mirror()

    def _disconnect_radio(self, index: int, detail: str) -> None:
        self._reset_memory_state()
        self._reset_split_state()
        self._stop_coordinator()
        try:
            if self.radios[index] is not None:
                self.radios[index].disable_auto_information()
        except Exception:
            pass
        self.engines[index].close()
        self.radios[index] = None
        self._connected[index] = False
        self._failed[index] = False
        self.status_vars[index].set("DISCONNECTED")
        self.detail_vars[index].set(detail)
        self._set_lamp(index, RED)
        self.cards[index]["connect"].configure(text="CONNECT")
        if index == 0:
            self._set_tx_indicator(None)
        self.model_vars[index].set("AWAITING CONNECTION")
        self._frequency_hz[index] = None
        self.freq_vars[index].set("")
        if self.layout_var.get() == "two":
            self.receiver_vars[index].set(Receiver.MAIN.value)
        if self.layout_var.get() == "same" and index == 0:
            self.radios[1] = None
            self.model_vars[1].set("AWAITING CONNECTION")
            self._frequency_hz[1] = None
            self.freq_vars[1].set("")
            self._connected[1] = False
            self.status_vars[1].set("SHARED CONNECTION")
            self.detail_vars[1].set("Uses Radio 1 COM connection.")
            self._set_lamp(1, RED)
        self._set_connection_controls(0, True)
        self._set_connection_controls(1, True)
        self._update_config_lock_state()
        self._refresh_routing_choices()
        self._update_mirror_availability()

    def _make_endpoints(self) -> tuple[RadioEndpoint, RadioEndpoint]:
        if self.layout_var.get() == "same":
            if not is_dual_receiver_radio(self.radios[0]):
                raise RuntimeError("Shared receiver mode requires a connected dual-receiver radio.")
            return (
                RadioEndpoint(self.radios[0], Receiver.MAIN, "Radio 1 / Main", lambda s: self._selection_from_thread(0, s)),
                RadioEndpoint(self.radios[0], Receiver.SUB, "Radio 2 / Sub", lambda s: self._selection_from_thread(1, s)),
            )
        endpoints = []
        for i in (0, 1):
            radio = self.radios[i]
            if radio is None:
                raise RuntimeError(f"Radio {i + 1} is not connected.")
            receiver = Receiver.MAIN if is_dual_receiver_radio(radio) else None
            endpoints.append(RadioEndpoint(
                radio, receiver, f"Radio {i + 1}",
                lambda selection, endpoint_index=i: self._selection_from_thread(endpoint_index, selection),
            ))
        return endpoints[0], endpoints[1]

    def _try_start_coordinator(self) -> None:
        if not all(self._connected):
            self._update_mirror_availability()
            return
        endpoints = self._make_endpoints()
        routing_enabled = self.layout_var.get() == "two"
        tx_action = "NO CHANGE"
        tx_source = self.tx_source_var.get()
        parking_frequency = 10_100_000
        if routing_enabled:
            supported = endpoints[1].supported_receive_sources()
            tx_action = self.tx_action_var.get()
            if tx_action == "SWITCH ANTENNA / RX PORT" and tx_source not in supported:
                tx_action = "NO CHANGE"
                self.tx_action_var.set(tx_action)
            if tx_action == "PARKING FREQUENCY":
                parking_frequency = self._parse_parking_frequency(self.parking_frequency_var.get())
        self.coordinator = MirrorCoordinator(
            endpoints, self._frequency_from_thread, self._failure_from_thread,
            poll_interval=0.05 if self.polling_var.get().startswith("FAST") else 0.10,
            transmit_callback=self._transmit_from_thread,
            route_callback=self._route_from_thread,
            routing_failure_callback=self._routing_failure_from_thread,
            operation_failure_callback=self._operation_failure_from_thread,
            range_callback=self._range_from_thread,
            tx_action=tx_action,
            tx_source=tx_source,
            parking_frequency=parking_frequency,
            frequency_ceiling_hz=MIRROR_FREQUENCY_CEILING_HZ,
            routing_enabled=routing_enabled,
            alignment_offsets=self._current_alignment_offsets(),
        )
        self.coordinator.set_source(self.master_var.get())
        self.coordinator.start_monitoring()
        self._update_mirror_availability()

    def _stop_coordinator(self) -> None:
        if not hasattr(self, "config_frame") or not self.config_frame.winfo_manager():
            self._set_skin("full", persist=False)
        coordinator = self.coordinator
        self.coordinator = None
        if coordinator is not None:
            coordinator.stop()
        self._mirror_active = False
        self._out_of_range = False
        if hasattr(self, "micro_range_badge"):
            self.micro_range_badge.configure(text="")
        if hasattr(self, "cards") and self.cards[1].get("range_badge") is not None:
            self.cards[1]["range_badge"].configure(text="")
            self._apply_display_scheme()
        self._reset_split_state()
        if hasattr(self, "mirror_button"):
            self.mirror_button.configure(text="START MIRROR")
        if hasattr(self, "mirror_lamp"):
            self.mirror_lamp.itemconfigure(self.mirror_lamp_dot, fill=RED, outline="#7f1d1d")
        if hasattr(self, "memory_button"):
            self.memory_button.configure(state="disabled")
        if hasattr(self, "micro_memory_button"):
            self.micro_memory_button.configure(state="disabled")
        if hasattr(self, "split_button"):
            self.split_button.configure(state="disabled")
        if hasattr(self, "micro_split_button"):
            self.micro_split_button.configure(state="disabled")
        self._set_tx_indicator(False)
        if hasattr(self, "status_vars"):
            for index in (0, 1):
                if self._connected[index]:
                    self.status_vars[index].set("CONNECTED • SHARED COM" if index == 1 and self.layout_var.get() == "same" else "CONNECTED")

    def _update_mirror_availability(self) -> None:
        state = "disabled" if self._quick_start_pending or any(self._busy) else "normal"
        self.mirror_button.configure(state=state)

    def _toggle_mirror(self) -> None:
        if not self._mirror_active and not all(self._connected):
            self._quick_connect_and_start()
            return
        if self.coordinator is None and all(self._connected):
            self._try_start_coordinator()
        if self.coordinator is None:
            self._show_config()
            return
        if self._mirror_active and self._transmitting:
            messagebox.showwarning(
                "Master Is Transmitting",
                "Return the Master to RX before stopping Mirror so the Sub remains diverted.",
                parent=self,
            )
            return
        if (self.layout_var.get() == "two" and not self._mirror_active
                and self.tx_action_var.get() != "NO CHANGE"
                and not self.safety_ack_var.get()):
            messagebox.showwarning(
                "Safety Acknowledgement Required",
                "Open CONFIG and acknowledge the Sub RF safety warning before enabling CAT diversion.",
                parent=self,
            )
            return
        self._mirror_active = not self._mirror_active
        self._routing_warning = ""
        if not self._mirror_active and self._memory_armed:
            self._disarm_memory()
        if not self._mirror_active and self._split_active:
            self._reset_split_state()
        self.coordinator.set_active(self._mirror_active)
        self.mirror_button.configure(text="STOP MIRROR" if self._mirror_active else "START MIRROR")
        self.mirror_lamp.itemconfigure(
            self.mirror_lamp_dot,
            fill=GREEN if self._mirror_active else RED,
            outline="#176b3d" if self._mirror_active else "#7f1d1d",
        )
        for index in (0, 1):
            self._set_lamp(index, GREEN)
            self.status_vars[index].set("MIRROR ACTIVE" if self._mirror_active else ("CONNECTED • SHARED COM" if index == 1 and self.layout_var.get() == "same" else "CONNECTED"))
        self.memory_button.configure(state="normal" if self._mirror_active else "disabled")
        self.micro_memory_button.configure(state="normal" if self._mirror_active else "disabled")
        self.split_button.configure(state="normal" if self._mirror_active else "disabled")
        self.micro_split_button.configure(state="normal" if self._mirror_active else "disabled")

    def _quick_connect_and_start(self) -> None:
        """Connect saved endpoints, then start Mirror without opening CONFIG."""
        self._refresh_ports()
        required = (0,) if self.layout_var.get() == "same" else (0, 1)
        problems = {}

        for index in required:
            if self._connected[index]:
                continue
            port = self.port_vars[index].get().strip()
            if not port:
                problems[index] = "No saved COM port. Select one in CONFIG."
                continue
            try:
                baud = int(self.baud_vars[index].get())
            except ValueError:
                problems[index] = "The saved baud rate is invalid. Select one in CONFIG."
                continue
            if baud not in SUPPORTED_BAUDS:
                problems[index] = "The saved baud rate is unsupported. Select one in CONFIG."

        if self.layout_var.get() == "two":
            left = self.port_vars[0].get().strip()
            right = self.port_vars[1].get().strip()
            if left and right and left == right:
                problems[1] = "Master and Sub cannot use the same COM port."

        if problems:
            for index, detail in problems.items():
                self._failed[index] = True
                self.status_vars[index].set("CONFIG REQUIRED")
                self.detail_vars[index].set(detail)
                self._set_lamp(index, RED)
            self._show_config()
            self._update_mirror_availability()
            return

        self._quick_start_pending = True
        self.mirror_button.configure(text="CONNECTING…", state="disabled")
        for index in required:
            if not self._connected[index] and not self._busy[index]:
                self._connect(index)
        self.mirror_button.configure(text="CONNECTING…", state="disabled")

    def _swap_direction(self) -> None:
        """Retained as a no-op for compatibility with older config/state."""
        return

    def _update_roles(self) -> None:
        master = 0
        self.master_var.set(master)
        for index in (0, 1):
            is_master = index == master
            self.role_vars[index].set("MASTER" if is_master else "SUB")
            self.cards[index]["frequency"].configure(font=("Consolas", 24, "bold"), cursor="sb_v_double_arrow" if is_master else "arrow")
        self._apply_display_scheme()

    def _apply_display_scheme(self) -> None:
        scheme = DISPLAY_SCHEMES.get(self._display_scheme, DISPLAY_SCHEMES["green_black"])
        master_fg, master_bg = scheme["master"]
        listener_fg, listener_bg = scheme["listener"]
        self.cards[0]["frequency"].configure(fg=master_fg, bg=master_bg)
        self.cards[1]["frequency"].configure(
            fg=MUTED if self._out_of_range else listener_fg, bg=listener_bg
        )
        self.micro_frequencies[0].configure(fg=master_fg, bg=master_bg)
        self.micro_frequencies[1].configure(
            fg=MUTED if self._out_of_range else listener_fg, bg=listener_bg
        )

    def _cycle_display_scheme(self, _event=None) -> str:
        try:
            current = DISPLAY_SCHEME_ORDER.index(self._display_scheme)
        except ValueError:
            current = -1
        self._display_scheme = DISPLAY_SCHEME_ORDER[(current + 1) % len(DISPLAY_SCHEME_ORDER)]
        self._apply_display_scheme()
        if hasattr(self, "config_display_var"):
            self.config_display_var.set(DISPLAY_SCHEME_NAMES[self._display_scheme])
        self._save_config()
        return "break"

    def _receiver_changed(self) -> None:
        if any(self._connected):
            return
        self._save_config()

    def _mouse_wheel(self, event: tk.Event, index: int) -> str:
        return self._tune_from_wheel(1 if event.delta > 0 else -1, index)

    def _tune_from_wheel(self, direction: int, index: int) -> str:
        master = self.master_var.get()
        if index != master or self.coordinator is None or not all(self._connected) or self._transmitting:
            return "break"
        current = self._wheel_frequency if self._wheel_frequency is not None else self._frequency_hz[master]
        if current is None:
            return "break"
        requested = directional_step(current, self.step_var.get(), direction)
        requested = min(MAX_FREQUENCY_HZ, max(MIN_FREQUENCY_HZ, requested))
        self._wheel_frequency = requested
        self._wheel_request_pending = requested
        if self._memory_armed:
            self._listen_frequency = requested
            self._update_memory_display()
        self._frequency_update(master, requested, authoritative=False)
        self.coordinator.request_tune(requested)
        return "break"

    def _frequency_from_thread(self, index: int, frequency_hz: int) -> None:
        self._post_ui(self._frequency_update, index, frequency_hz)

    def _range_from_thread(self, out_of_range: bool, frequency_hz: int) -> None:
        self._post_ui(self._range_state_changed, out_of_range, frequency_hz)

    def _range_state_changed(self, out_of_range: bool, frequency_hz: int) -> None:
        self._out_of_range = out_of_range
        if out_of_range:
            self.freq_vars[1].set(format_frequency(frequency_hz))
            self.cards[1]["frequency"].configure(fg=MUTED)
            self.micro_frequencies[1].configure(fg=MUTED)
            self.cards[1]["range_badge"].configure(text="●")
            self.micro_range_badge.configure(text="● >30 MHz")
            self.status_vars[1].set("MIRROR PAUSED • ABOVE 30 MHz")
            self.memory_button.configure(state="disabled")
            self.micro_memory_button.configure(state="disabled")
            self.split_button.configure(state="disabled")
            self.micro_split_button.configure(state="disabled")
            return
        self.cards[1]["range_badge"].configure(text="")
        self.micro_range_badge.configure(text="")
        self._apply_display_scheme()
        if self._mirror_active:
            self.status_vars[1].set("MIRROR ACTIVE")
        self._update_split_display()
        self._update_memory_display()

    def _selection_from_thread(self, index: int, selection: str) -> None:
        self._post_ui(self._selection_update, index, selection)

    def _selection_update(self, index: int, selection: str) -> None:
        self.receiver_vars[index].set(selection)

    def _transmit_from_thread(self, transmitting: bool) -> None:
        self._post_ui(self._transmit_state_changed, transmitting)

    def _route_from_thread(self, transmitting: bool, source: str) -> None:
        self._post_ui(self._route_state_changed, transmitting, source)

    def _routing_failure_from_thread(self, message: str) -> None:
        self._post_ui(self._routing_failed, message)

    def _operation_failure_from_thread(self, message: str) -> None:
        self._post_ui(self._operation_failed, message)

    def _routing_failed(self, message: str) -> None:
        """Antenna routing is optional; keep both healthy CAT links open."""
        self._routing_warning = message
        self._mirror_active = False
        self._reset_memory_state(preserve_transmit=True)
        self._reset_split_state()
        self._set_skin("full", persist=False)
        self.mirror_button.configure(text="START MIRROR")
        self.mirror_lamp.itemconfigure(self.mirror_lamp_dot, fill=AMBER, outline="#8a5b12")
        self.memory_button.configure(state="disabled")
        self.micro_memory_button.configure(state="disabled")
        self.split_button.configure(state="disabled")
        self.micro_split_button.configure(state="disabled")
        if self._connected[0]:
            self.status_vars[0].set("CONNECTED")
            self._set_lamp(0, GREEN)
        if self._connected[1]:
            self.status_vars[1].set("ROUTING FAILED")
            self.detail_vars[1].set(message)
            self._set_lamp(1, AMBER)
        messagebox.showwarning("Sub Routing Failed", message, parent=self)

    def _operation_failed(self, message: str) -> None:
        """A rejected command pauses Mirror without pretending a CAT link was lost."""
        self._routing_warning = message
        self._mirror_active = False
        self._reset_memory_state(preserve_transmit=True)
        self._reset_split_state()
        self._set_skin("full", persist=False)
        self.mirror_button.configure(text="START MIRROR")
        self.mirror_lamp.itemconfigure(self.mirror_lamp_dot, fill=AMBER, outline="#8a5b12")
        self.memory_button.configure(state="disabled")
        self.micro_memory_button.configure(state="disabled")
        self.split_button.configure(state="disabled")
        self.micro_split_button.configure(state="disabled")
        for index in (0, 1):
            if self._connected[index]:
                self.status_vars[index].set("CONNECTED • MIRROR PAUSED")
                self._set_lamp(index, GREEN)
        self.detail_vars[self.master_var.get()].set(message)
        messagebox.showwarning("Mirror Operation Failed", message, parent=self)

    def _route_state_changed(self, transmitting: bool, source: str) -> None:
        if not self._mirror_active:
            return
        if transmitting:
            suffix = " • ABOVE 30 MHz" if self._out_of_range else ""
            self.status_vars[1].set(f"TX • {source}{suffix}")
        elif self._out_of_range:
            self.status_vars[1].set("MIRROR PAUSED • ABOVE 30 MHz")
        elif self._split_active:
            dx = format_frequency(self._split_frequency) if self._split_frequency is not None else "--.---.---"
            self.status_vars[1].set(f"DX HOLD • {dx} • {source}")
        else:
            self.status_vars[1].set(f"MIRROR ACTIVE • {source}")

    def _frequency_update(self, index: int, frequency_hz: int, authoritative: bool = True) -> None:
        previous_hz = self._frequency_hz[index]
        self._frequency_hz[index] = frequency_hz
        if not (index == 1 and self._out_of_range):
            self.freq_vars[index].set(format_frequency(frequency_hz))
        if index == self.master_var.get() and self.__dict__.get("_split_active", False):
            self._update_split_display()
        if ("alignment_frame" in self.__dict__
                and self.alignment_frame.winfo_manager()):
            self._refresh_alignment_display()
            if index == self.master_var.get() and authoritative:
                self._preview_alignment_on_sub()
        if index != self.master_var.get() or not authoritative:
            return

        pending = self._wheel_request_pending
        if pending is not None:
            if frequency_hz == pending:
                self._wheel_request_pending = None
                self._wheel_frequency = frequency_hz
            return

        if (self._memory_armed and not self._transmitting
                and previous_hz is not None
                and tuning_band(previous_hz) != tuning_band(frequency_hz)):
            self._abandon_memory_at(frequency_hz)
        split_active = self.__dict__.get("_split_active", False)
        split_frequency = self.__dict__.get("_split_frequency")
        if (split_active and split_frequency is not None
                and tuning_band(split_frequency) != tuning_band(frequency_hz)):
            self._abandon_split_at(frequency_hz)
        self._wheel_frequency = frequency_hz

    def _failure_from_thread(self, index: int, message: str) -> None:
        self._post_ui(self._connection_failed, index, message)

    def _connection_failed(self, index: int, message: str) -> None:
        self._reset_memory_state()
        self._reset_split_state()
        self._stop_coordinator()
        affected = (0, 1) if self.layout_var.get() == "same" or index not in (0, 1) else (index,)
        for failed_index in affected:
            engine_index = 0 if self.layout_var.get() == "same" else failed_index
            self.engines[engine_index].close()
            self.radios[failed_index] = None
            self._connected[failed_index] = False
            self._failed[failed_index] = True
            self.status_vars[failed_index].set("CONNECTION FAILED")
            self.detail_vars[failed_index].set(message)
            self._set_lamp(failed_index, RED)
            self.model_vars[failed_index].set("")
            self.receiver_vars[failed_index].set("")
            self._frequency_hz[failed_index] = None
            self.freq_vars[failed_index].set("")
            self.cards[failed_index]["connect"].configure(text="RETRY")
        for survivor in set((0, 1)).difference(affected):
            if self._connected[survivor]:
                self.status_vars[survivor].set("CONNECTED • MIRROR PAUSED")
                self.detail_vars[survivor].set("The other radio stopped responding.")
                self._set_lamp(survivor, GREEN)
        if 0 in affected:
            self._set_tx_indicator(None)
        self._out_of_range = False
        self.cards[1]["range_badge"].configure(text="")
        self.micro_range_badge.configure(text="")
        self._apply_display_scheme()
        for control_index in (0, 1):
            self._set_connection_controls(control_index, True)
        self._update_config_lock_state()
        self._update_mirror_availability()

    def _toggle_memory(self) -> None:
        if (self.coordinator is None or not self._mirror_active
                or self._split_active or self._out_of_range or self._transmitting):
            return
        if self._memory_armed:
            self._disarm_memory()
            return
        master = self.master_var.get()
        current = self._frequency_hz[master]
        if current is None:
            return
        self._memory_armed = True
        self._home_frequency = current
        self._listen_frequency = current
        self._pre_memory_step = self.step_var.get()
        self.step_var.set(10)
        self.coordinator.arm_memory(current)
        self._update_memory_display()

    def _toggle_split(self) -> None:
        coordinator = self.coordinator
        if (coordinator is None or not self._mirror_active
                or self._transmitting or self._out_of_range):
            return
        if self._split_active:
            dx_frequency = coordinator.return_from_split()
            self._split_active = False
            self._split_frequency = None
            if dx_frequency is not None:
                self._wheel_frequency = dx_frequency
                self._wheel_request_pending = dx_frequency
                self._frequency_update(self.master_var.get(), dx_frequency, authoritative=False)
            self._update_split_display()
            for index in (0, 1):
                self.status_vars[index].set("MIRROR ACTIVE")
            return

        master = self.master_var.get()
        current = self._frequency_hz[master]
        if current is None:
            return
        if self._memory_armed:
            self._abandon_memory_at(current)
        self._split_active = True
        self._split_frequency = current
        shifted = min(MAX_FREQUENCY_HZ, current + SPLIT_OFFSET_HZ)
        self._wheel_frequency = shifted
        self._wheel_request_pending = shifted
        coordinator.start_split(current, shifted - current)
        self._frequency_update(master, shifted, authoritative=False)
        self._update_split_display()

    def _abandon_split_at(self, frequency_hz: int) -> None:
        """Leave Split on a band change and mirror the new Master frequency."""
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.abandon_split()
        self._split_active = False
        self._split_frequency = None
        self._wheel_frequency = frequency_hz
        self._wheel_request_pending = None
        self._update_split_display()
        for index in (0, 1):
            self.status_vars[index].set("MIRROR ACTIVE")

    def _reset_split_state(self) -> None:
        coordinator = self.coordinator
        if self._split_active and coordinator is not None:
            coordinator.abandon_split()
        self._split_active = False
        self._split_frequency = None
        if hasattr(self, "split_button"):
            self._update_split_display()

    def _update_split_display(self) -> None:
        if not hasattr(self, "split_button") or not hasattr(self, "micro_split_button"):
            return
        split_buttons = (self.split_button, self.micro_split_button)
        active_colour = "#17120a"
        if self._split_active:
            for button in split_buttons:
                button.configure(
                    text="RETURN", bg=AMBER, activebackground=AMBER,
                    fg=active_colour, activeforeground=active_colour,
                    state="disabled" if self._transmitting else "normal",
                )
            self.memory_button.configure(state="disabled")
            self.micro_memory_button.configure(state="disabled")
            dx_frequency = self._split_frequency
            current = self._frequency_hz[self.master_var.get()]
            if dx_frequency is not None and current is not None:
                difference_khz = (current - dx_frequency) / 1_000
                self.status_vars[0].set(f"SPLIT • FREE VFO • {difference_khz:+.1f} kHz")
                if not self._transmitting:
                    self.status_vars[1].set(f"DX HOLD • {format_frequency(dx_frequency)}")
            return

        state = "normal" if self._mirror_active and not self._transmitting and not self._out_of_range else "disabled"
        for button in split_buttons:
            button.configure(
                text="SPLIT +5", bg=FIELD, activebackground=FIELD,
                fg=TEXT, activeforeground=TEXT, state=state,
            )
        memory_state = "normal" if self._mirror_active and not self._out_of_range else "disabled"
        self.memory_button.configure(state=memory_state)
        self.micro_memory_button.configure(state=memory_state)

    def _disarm_memory(self) -> None:
        coordinator = self.coordinator
        home = self._home_frequency
        if coordinator is not None:
            coordinator.cancel_memory()
        self._memory_armed = False
        if home is not None:
            self._wheel_frequency = home
            self._wheel_request_pending = home
        self._home_frequency = None
        self._listen_frequency = None
        self._restore_pre_memory_step()
        self._update_memory_display()

    def _abandon_memory_at(self, frequency_hz: int) -> None:
        """Forget an obsolete M home after the operator changes bands."""
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.abandon_memory()
        self._memory_armed = False
        self._home_frequency = None
        self._listen_frequency = None
        self._wheel_frequency = frequency_hz
        self._wheel_request_pending = None
        self._restore_pre_memory_step()
        self._update_memory_display()

    def _reset_memory_state(self, preserve_transmit: bool = False) -> None:
        self._memory_armed = False
        self._home_frequency = None
        self._listen_frequency = None
        self._wheel_request_pending = None
        self._restore_pre_memory_step()
        if not preserve_transmit:
            self._transmitting = False
        if hasattr(self, "memory_button"):
            self.memory_button.configure(state="disabled")
            self.micro_memory_button.configure(state="disabled")
            self._update_memory_display()

    def _restore_pre_memory_step(self) -> None:
        if self._pre_memory_step in (1000, 500, 100, 10):
            self.step_var.set(self._pre_memory_step)
        self._pre_memory_step = None

    def _update_memory_display(self) -> None:
        if not self._memory_armed:
            self.memory_button.configure(bg=FIELD, activebackground=FIELD, fg=TEXT)
            self.micro_memory_button.configure(bg=FIELD, activebackground=FIELD, fg=TEXT)
            self.memory_status.configure(text="TX/RX OFFSET OFF", foreground=TEXT)
            self.micro_memory_status.configure(text="OFFSET OFF", foreground=TEXT)
            return
        home = format_frequency(self._home_frequency) if self._home_frequency is not None else "--.---.---"
        listen = format_frequency(self._listen_frequency) if self._listen_frequency is not None else home
        state = "TX" if self._transmitting else "RX"
        self.memory_button.configure(bg=AMBER, activebackground=AMBER, fg="#17120a")
        self.micro_memory_button.configure(bg=AMBER, activebackground=AMBER, fg="#17120a")
        self.memory_status.configure(text=f"M • {state}   TX {home}   RX {listen}", foreground=AMBER)
        self.micro_memory_status.configure(text=f"M {state}", foreground=AMBER)

    def _information_from_thread(self, engine_index: int, command: str) -> None:
        if command.startswith("TX"):
            transmitting = True
        elif command == "RX;":
            transmitting = False
        else:
            return
        # MASTER is permanently Radio 1, so no Tk variable needs to be read
        # from this serial callback thread.
        if engine_index != 0:
            return
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.set_transmitting(transmitting)

    def _transmit_state_changed(self, transmitting: bool) -> None:
        self._transmitting = transmitting
        self._set_tx_indicator(transmitting)
        self._update_alignment_button_state()
        if self._memory_armed:
            self._update_memory_display()
        self._update_split_display()

    def _set_tx_indicator(self, transmitting: Optional[bool]) -> None:
        if transmitting is None:
            text, background, foreground = "--", "#4b5563", "#eef1f4"
        elif transmitting:
            text, background, foreground = "TX", "#b91c1c", "#fff4f4"
        else:
            text, background, foreground = "RX", "#176b3d", "#f4fff8"
        badges = [self.micro_tx_badge]
        full_badge = self.cards[0].get("tx_badge")
        if full_badge is not None:
            badges.append(full_badge)
        for badge in badges:
            badge.configure(text=text, bg=background, fg=foreground)

    def _set_connection_controls(self, index: int, enabled: bool) -> None:
        shared_listener = index == 1 and self.layout_var.get() == "same"
        editable = enabled and not self._connected[index] and not shared_listener
        self.cards[index]["port"].configure(state="normal" if editable else "disabled")
        self.cards[index]["baud"].configure(state="readonly" if editable else "disabled")
        self.cards[index]["civ_entry"].configure(state="normal" if editable else "disabled")
        self.cards[index]["refresh"].configure(state="normal" if editable else "disabled")
        self.cards[index]["connect"].configure(
            state="normal" if enabled and not shared_listener else "disabled"
        )
        self.cards[index]["load_driver"].configure(
            state="normal" if editable else "disabled"
        )
        self.cards[index]["built_in_driver"].configure(
            state=(
                "normal" if editable and self._driver_data[index] is not None
                else "disabled"
            )
        )
        self.cards[index]["test_driver"].configure(
            state=(
                "normal" if editable and self._driver_data[index] is not None
                else "disabled"
            )
        )

    def _update_config_lock_state(self) -> None:
        state = "disabled" if any(self._connected) or any(self._busy) else "normal"
        for button in self.layout_buttons:
            button.configure(state=state)
        self._update_alignment_button_state()

    def _update_alignment_button_state(self) -> None:
        if not hasattr(self, "alignment_button"):
            return
        available = all(self._connected) and not self._mirror_active and not self._transmitting
        self.alignment_button.configure(state="normal" if available else "disabled")

    def _set_lamp(self, index: int, colour: str) -> None:
        self.cards[index]["lamp"].itemconfigure(self.cards[index]["lamp_dot"], fill=colour)
        config_lamp = self.cards[index].get("config_lamp")
        config_lamp_dot = self.cards[index].get("config_lamp_dot")
        if config_lamp is not None and config_lamp_dot is not None:
            config_lamp.itemconfigure(config_lamp_dot, fill=colour)

    def _post_ui(self, callback, *args) -> None:
        """Queue GUI work without ever entering Tk from a worker thread."""
        if not self._closing:
            self._ui_events.put((callback, args))

    def _drain_ui_events(self) -> None:
        """Run queued radio events exclusively on Tk's main thread."""
        if self._closing:
            return
        processed = 0
        try:
            while processed < 500:
                try:
                    callback, args = self._ui_events.get_nowait()
                except queue.Empty:
                    break
                callback(*args)
                processed += 1
        finally:
            if not self._closing:
                self._ui_pump_id = self.after(15, self._drain_ui_events)

    def _toggle_diagnostics(self) -> None:
        self._show_diagnostics()

    def _set_skin(self, skin: str, persist: bool = True) -> None:
        if skin not in ("full", "micro"):
            return
        if skin == "micro" and (not all(self._connected) or not self._mirror_active):
            if persist:
                messagebox.showinfo(
                    "Start Mirror First",
                    "Connect both radios and start Mirror before entering Micro mode.",
                    parent=self,
                )
            return

        if persist:
            self._skin_preference = skin
        if skin == "micro":
            self._diagnostics_visible = False
            self.config_frame.pack_forget()
            for widget in (self.header_frame, self.body_frame, self.tune_frame):
                widget.pack_forget()
            self.root_frame.configure(padding=4)
            self.micro_frame.pack(fill="x")
            self.minsize(420, 112)
            self.geometry("450x126")
            self.update_idletasks()
            micro_width = max(430, self.root_frame.winfo_reqwidth())
            self.minsize(micro_width, 112)
            self.geometry(f"{micro_width}x126")
        else:
            self._show_main()
        if persist:
            self._save_config()

    def _traffic_from_thread(self, index: int, direction: str, command: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S%z")
        tenth = int(time.time() * 10) % 10
        line = f"{stamp}.{tenth}  R{index + 1}  {direction:<2}  {command}\n"
        self._post_ui(self._append_console, line)

    def _append_console(self, line: str) -> None:
        self._diagnostics_lines.append(line)
        if self._diagnostics_window is None or not self._diagnostics_window.winfo_exists():
            return
        if self._diagnostics_paused:
            return
        self.console.configure(state="normal")
        self.console.insert("end", line)
        if int(self.console.index("end-1c").split(".")[0]) > 5000:
            self.console.delete("1.0", "2.0")
        if self._diagnostics_autoscroll is None or self._diagnostics_autoscroll.get():
            self.console.see("end")
        self.console.configure(state="disabled")

    def _clear_console(self) -> None:
        self._diagnostics_lines.clear()
        if self._diagnostics_window is not None and self._diagnostics_window.winfo_exists():
            self.console.configure(state="normal")
            self.console.delete("1.0", "end")
            self.console.configure(state="disabled")

    def _show_diagnostics(self) -> None:
        if self._diagnostics_window is not None and self._diagnostics_window.winfo_exists():
            self._diagnostics_window.deiconify()
            self._diagnostics_window.lift()
            self._diagnostics_window.focus_force()
            return

        window = tk.Toplevel(self)
        self._diagnostics_window = window
        self._diagnostics_visible = True
        self._diagnostics_paused = False
        window.title("RigMirror CAT Diagnostics")
        window.geometry("900x500")
        window.minsize(620, 300)
        window.configure(bg=BG)
        window.protocol("WM_DELETE_WINDOW", self._close_diagnostics)

        frame = ttk.Frame(window, style="App.TFrame", padding=8)
        frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame, style="App.TFrame")
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(toolbar, text="CAT DIAGNOSTICS", style="App.TLabel", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(toolbar, text="Last 5,000 lines retained", style="Muted.TLabel").pack(side="left", padx=10)
        ttk.Button(toolbar, text="TEST MASTER PORT", command=lambda: self._test_port(0)).pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="TEST SUB PORT", command=lambda: self._test_port(1)).pack(side="left", padx=(4, 0))
        self._diagnostics_autoscroll = tk.BooleanVar(master=window, value=True)
        ttk.Checkbutton(
            toolbar, text="AUTO-SCROLL", variable=self._diagnostics_autoscroll,
            style="App.TCheckbutton",
        ).pack(side="right", padx=(6, 0))
        ttk.Button(toolbar, text="CLEAR", command=self._clear_console).pack(side="right", padx=(6, 0))
        ttk.Button(toolbar, text="SAVE LOG…", command=self._save_diagnostics).pack(side="right", padx=(6, 0))
        ttk.Button(toolbar, text="COPY", command=self._copy_diagnostics).pack(side="right", padx=(6, 0))
        self.diagnostics_pause_button = ttk.Button(
            toolbar, text="PAUSE", command=self._toggle_diagnostics_pause,
        )
        self.diagnostics_pause_button.pack(side="right")

        console_frame = ttk.Frame(frame, style="App.TFrame")
        console_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(console_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.console = tk.Text(
            console_frame, bg="#0b0e11", fg="#d4f8e3",
            insertbackground=TEXT, relief="flat", font=("Consolas", 9),
            padx=8, pady=6, state="disabled", wrap="none",
            yscrollcommand=scrollbar.set,
        )
        self.console.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.console.yview)
        self._refresh_diagnostics_console()

    def _close_diagnostics(self) -> None:
        window = self._diagnostics_window
        self._diagnostics_window = None
        self._diagnostics_visible = False
        self._diagnostics_paused = False
        self._diagnostics_autoscroll = None
        if window is not None and window.winfo_exists():
            window.destroy()

    def _toggle_diagnostics_pause(self) -> None:
        self._diagnostics_paused = not self._diagnostics_paused
        self.diagnostics_pause_button.configure(
            text="RESUME" if self._diagnostics_paused else "PAUSE"
        )
        if not self._diagnostics_paused:
            self._refresh_diagnostics_console()

    def _refresh_diagnostics_console(self) -> None:
        if self._diagnostics_window is None or not self._diagnostics_window.winfo_exists():
            return
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.insert("end", "".join(self._diagnostics_lines))
        if self._diagnostics_autoscroll is None or self._diagnostics_autoscroll.get():
            self.console.see("end")
        self.console.configure(state="disabled")

    def _copy_diagnostics(self) -> None:
        self.clipboard_clear()
        self.clipboard_append("".join(self._diagnostics_lines))
        self.update_idletasks()

    def _save_diagnostics(self) -> None:
        filename = filedialog.asksaveasfilename(
            parent=self._diagnostics_window or self,
            title="Save CAT Diagnostics",
            defaultextension=".txt",
            initialfile=time.strftime("RigMirror_CAT_%Y%m%d_%H%M%S.txt"),
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if not filename:
            return
        try:
            Path(filename).write_text("".join(self._diagnostics_lines), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Save Failed", str(exc), parent=self._diagnostics_window or self)

    def _show_config(self) -> None:
        self._config_snapshot = {
            "layout": self.layout_var.get(),
            "ports": [variable.get() for variable in self.port_vars],
            "bauds": [variable.get() for variable in self.baud_vars],
            "driver_paths": list(self._driver_paths),
            "driver_data": list(self._driver_data),
            "civ_addresses": [v.get() for v in self.civ_vars],
            "tx_action": self.tx_action_var.get(),
            "tx_source": self.tx_source_var.get(),
            "parking_frequency": self.parking_frequency_var.get(),
            "polling": self.polling_var.get(),
        }
        self.config_tx_var.set(self.tx_source_var.get())
        self.config_action_var.set(self.tx_action_var.get())
        self.config_polling_var.set(self.polling_var.get())
        self._refresh_routing_choices()
        self.config_safety_var.set(self.safety_ack_var.get())
        self.config_display_var.set(DISPLAY_SCHEME_NAMES.get(self._display_scheme, "Green on black"))
        self._update_config_lock_state()
        self._set_connection_controls(0, not self._busy[0])
        self._set_connection_controls(1, not self._busy[1])

        self.root_frame.configure(padding=8)
        self.header_frame.pack_forget()
        self.body_frame.pack_forget()
        self.tune_frame.pack_forget()
        self.micro_frame.pack_forget()
        self.alignment_frame.pack_forget()
        self.config_frame.pack(fill="both", expand=True)
        self.minsize(900, 570)
        self.geometry("980x610")

    def _current_alignment_signature(self) -> Optional[str]:
        if not all(self._connected) or self.radios[0] is None:
            return None
        master_model = self.radios[0].identity.model
        master_port = self.port_vars[0].get()
        if self.layout_var.get() == "same":
            sub_model = master_model
            sub_port = master_port
        else:
            if self.radios[1] is None:
                return None
            sub_model = self.radios[1].identity.model
            sub_port = self.port_vars[1].get()
        return alignment_profile_signature(
            self.layout_var.get(), master_model, master_port, sub_model, sub_port,
        )

    def _current_alignment_offsets(self) -> dict[str, int]:
        signature = self._current_alignment_signature()
        return dict(self._alignment_profiles.get(signature, {})) if signature else {}

    def _show_alignment(self) -> None:
        if not all(self._connected):
            messagebox.showinfo(
                "Connect Both Radios",
                "Connect Master and Sub before opening SUB VFO Alignment.",
                parent=self,
            )
            return
        if self._mirror_active:
            messagebox.showinfo(
                "Stop Mirror First",
                "Stop Mirror before adjusting SUB VFO Alignment.",
                parent=self,
            )
            return
        if self._transmitting:
            messagebox.showwarning(
                "Master Is Transmitting",
                "Return the Master to RX before adjusting alignment.",
                parent=self,
            )
            return
        master_hz = self._frequency_hz[self.master_var.get()]
        if master_hz is None or band_for_frequency(master_hz) is None:
            messagebox.showinfo(
                "Tune To An Amateur Band",
                "Tune the Master to 160, 80, 60, 40, 30, 20, 17, 15, 12, 10 or 6 metres.",
                parent=self,
            )
            return
        if self.coordinator is None:
            self._try_start_coordinator()
        if self.coordinator is None:
            messagebox.showerror("Alignment Unavailable", "The CAT monitor is not ready.", parent=self)
            return

        signature = self._current_alignment_signature()
        if signature is None:
            return
        self._alignment_signature = signature
        self._alignment_snapshot = self._current_alignment_offsets()
        self._alignment_preview = dict(self._alignment_snapshot)
        self.coordinator.set_alignment_offsets(self._alignment_preview)
        self._preview_alignment_on_sub()
        self._refresh_alignment_display()

        self.config_frame.pack_forget()
        self.alignment_frame.pack(fill="both", expand=True)
        self.minsize(720, 390)
        self.geometry("780x430")

    def _alignment_band(self):
        master_hz = self._frequency_hz[self.master_var.get()]
        return band_for_frequency(master_hz) if master_hz is not None else None

    def _refresh_alignment_display(self) -> None:
        band = self._alignment_band()
        if band is None:
            self.alignment_band_var.set("OUTSIDE SUPPORTED AMATEUR BANDS")
            self.alignment_offset_var.set("SUB ALIGNMENT UNAVAILABLE")
            return
        offsets = self._alignment_preview or {}
        offset = int(offsets.get(band.key, 0))
        self.alignment_band_var.set(band.label)
        self.alignment_offset_var.set(f"SUB = MASTER {offset:+d} Hz")

    def _preview_alignment_on_sub(self) -> None:
        if self.coordinator is None or self._alignment_preview is None or self._transmitting:
            return
        master_hz = self._frequency_hz[self.master_var.get()]
        if master_hz is None or band_for_frequency(master_hz) is None:
            return
        desired_hz = aligned_sub_frequency(master_hz, self._alignment_preview)
        if self._frequency_hz[1] != desired_hz:
            self.coordinator.request_sub_frequency(desired_hz)

    def _adjust_alignment(self, delta_hz: int) -> None:
        if self._transmitting:
            return
        band = self._alignment_band()
        if band is None or self._alignment_preview is None:
            return
        current = int(self._alignment_preview.get(band.key, 0))
        changed = max(-MAX_ALIGNMENT_HZ, min(MAX_ALIGNMENT_HZ, current + delta_hz))
        if changed:
            self._alignment_preview[band.key] = changed
        else:
            self._alignment_preview.pop(band.key, None)
        self._alignment_preview = normalise_offsets(self._alignment_preview)
        if self.coordinator is not None:
            self.coordinator.set_alignment_offsets(self._alignment_preview)
        self._preview_alignment_on_sub()
        self._refresh_alignment_display()

    def _reset_alignment(self) -> None:
        band = self._alignment_band()
        if band is None or self._alignment_preview is None:
            return
        self._alignment_preview.pop(band.key, None)
        if self.coordinator is not None:
            self.coordinator.set_alignment_offsets(self._alignment_preview)
        self._preview_alignment_on_sub()
        self._refresh_alignment_display()

    def _reset_all_alignments(self) -> None:
        if self._alignment_preview is None:
            return
        if not messagebox.askyesno(
            "Reset All Alignments",
            ("Preview 0 Hz on every amateur band for this Master/Sub pairing? "
             "Press SET afterwards to save the reset."),
            parent=self,
        ):
            return
        self._alignment_preview = {}
        if self.coordinator is not None:
            self.coordinator.set_alignment_offsets({})
        self._preview_alignment_on_sub()
        self._refresh_alignment_display()

    def _return_to_config_from_alignment(self) -> None:
        self.alignment_frame.pack_forget()
        self.config_frame.pack(fill="both", expand=True)
        self.minsize(900, 570)
        self.geometry("980x610")
        self._alignment_signature = None
        self._alignment_snapshot = None
        self._alignment_preview = None

    def _set_alignment(self) -> None:
        signature = self._alignment_signature
        if signature is None or self._alignment_preview is None:
            return
        clean = normalise_offsets(self._alignment_preview)
        if clean:
            self._alignment_profiles[signature] = clean
        else:
            self._alignment_profiles.pop(signature, None)
        if self.coordinator is not None:
            self.coordinator.set_alignment_offsets(clean)
        self._save_config()
        self._return_to_config_from_alignment()

    def _cancel_alignment(self) -> None:
        original = dict(self._alignment_snapshot or {})
        if self.coordinator is not None:
            self.coordinator.set_alignment_offsets(original)
        self._alignment_preview = original
        self._preview_alignment_on_sub()
        self._return_to_config_from_alignment()

    def _apply_config(self) -> None:
        tx_action = self.config_action_var.get()
        tx_source = self.config_tx_var.get()
        try:
            parking_frequency = self._parse_parking_frequency(self.parking_frequency_var.get())
        except (ValueError, TypeError) as exc:
            if tx_action == "PARKING FREQUENCY":
                messagebox.showerror("Invalid Parking Frequency", str(exc), parent=self)
                return
            parking_frequency = 10_100_000
        if (self.layout_var.get() == "two" and tx_action != "NO CHANGE"
                and not self.config_safety_var.get()):
            messagebox.showwarning(
                "Please Acknowledge",
                "Please acknowledge the RF safety warning before selecting CAT diversion.",
                parent=self,
            )
            return
        self.tx_action_var.set(tx_action)
        self.tx_source_var.set(tx_source)
        self.parking_frequency_var.set(format_frequency(parking_frequency))
        polling = self.config_polling_var.get()
        if polling not in ("NORMAL • 100 ms", "FAST • 50 ms"):
            polling = "NORMAL • 100 ms"
        self.polling_var.set(polling)
        self.safety_ack_var.set(self.config_safety_var.get())
        display_name = self.config_display_var.get()
        self._display_scheme = DISPLAY_SCHEME_LABELS.get(display_name, "green_black")
        self._apply_display_scheme()
        if self.coordinator is not None:
            self.coordinator.set_routing(tx_action, tx_source, parking_frequency)
            self.coordinator.set_poll_interval(0.05 if polling.startswith("FAST") else 0.10)
        self._save_config()
        self._config_snapshot = None
        self._show_main()

    def _cancel_config(self) -> None:
        snapshot = getattr(self, "_config_snapshot", None)
        if snapshot is not None:
            self.layout_var.set(snapshot["layout"])
            for index in (0, 1):
                self.port_vars[index].set(snapshot["ports"][index])
                self.baud_vars[index].set(snapshot["bauds"][index])
                self._driver_paths[index] = snapshot["driver_paths"][index]
                self._driver_data[index] = snapshot["driver_data"][index]
                self.civ_vars[index].set(snapshot.get("civ_addresses", ["", ""])[index])
                self._show_driver_choice(index)
            self.tx_action_var.set(snapshot.get("tx_action", "NO CHANGE"))
            self.tx_source_var.set(snapshot.get("tx_source", "ANT2"))
            self.parking_frequency_var.set(snapshot.get("parking_frequency", "10.100.000"))
            self.polling_var.set(snapshot.get("polling", "NORMAL • 100 ms"))
            self._layout_changed(initial=True)
        self._config_snapshot = None
        self._show_main()

    def _show_main(self) -> None:
        self.root_frame.configure(padding=8)
        self.config_frame.pack_forget()
        self.micro_frame.pack_forget()
        self.alignment_frame.pack_forget()
        self.header_frame.pack(fill="x", pady=(0, 8))
        self.body_frame.pack(fill="x")
        self.tune_frame.pack(fill="x", pady=(7, 0))
        self.minsize(760, 285)
        self.geometry("820x315")

    def _load_config(self) -> None:
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            self.layout_var.set(data.get("layout", DEFAULT_LAYOUT) if data.get("layout") in ("same", "two") else DEFAULT_LAYOUT)
            ports = data.get("ports", ["", ""])
            bauds = data.get("bauds", [DEFAULT_BAUD, DEFAULT_BAUD])
            receivers = data.get("receivers", ["MAIN", "SUB"])
            for i in (0, 1):
                self.port_vars[i].set(str(ports[i]))
                baud = int(bauds[i])
                if baud in SUPPORTED_BAUDS:
                    self.baud_vars[i].set(str(baud))
                if receivers[i] in ("MAIN", "SUB"):
                    self.receiver_vars[i].set(receivers[i])
            step = int(data.get("step_hz", 1000))
            if step in (1000, 500, 100, 10):
                self.step_var.set(step)
            self.tx_source_var.set(str(data.get("tx_source", "NO CHANGE")))
            self.tx_action_var.set(str(data.get("tx_action", "NO CHANGE")))
            self.parking_frequency_var.set(str(data.get("parking_frequency", "10.100.000")))
            polling = str(data.get("polling", "NORMAL • 100 ms"))
            self.polling_var.set(
                polling if polling in ("NORMAL • 100 ms", "FAST • 50 ms")
                else "NORMAL • 100 ms"
            )
            self.safety_ack_var.set(bool(data.get("safety_acknowledged", False)))
            self._skin_preference = str(data.get("skin", "full"))
            if self._skin_preference not in ("full", "micro"):
                self._skin_preference = "full"
            display_scheme = str(data.get("display_scheme", "green_black"))
            self._display_scheme = display_scheme if display_scheme in DISPLAY_SCHEMES else "green_black"
            profiles = data.get("alignment_profiles", {})
            if isinstance(profiles, dict):
                self._alignment_profiles = {
                    str(signature): normalise_offsets(offsets)
                    for signature, offsets in profiles.items()
                    if normalise_offsets(offsets)
                }
            history = data.get("driver_test_history", {})
            if isinstance(history, dict):
                self._driver_test_history = {
                    str(key): value for key, value in history.items()
                    if isinstance(value, dict)
                }
            drivers = data.get("drivers", ["", ""])
            if not isinstance(drivers, list) or len(drivers) < 2:
                drivers = ["", ""]
            for index in (0, 1):
                path = self._resolved_driver_path(str(drivers[index]))
                if path is None:
                    continue
                try:
                    driver = load_driver(path)
                except Exception:
                    self._driver_paths[index] = None
                    self._driver_data[index] = None
                    self.driver_name_vars[index].set("DRIVER NOT FOUND")
                    self.driver_status_vars[index].set(str(path))
                else:
                    self._driver_paths[index] = path
                    self._driver_data[index] = driver
                    addresses = data.get("civ_addresses", ["", ""])
                    self.civ_vars[index].set(addresses[index] or str(driver.get("transport", {}).get("civ_address", "")))
                    self._show_driver_choice(index)
            self.master_var.set(0)
        except (OSError, ValueError, TypeError, IndexError, json.JSONDecodeError):
            pass

    def _save_config(self) -> None:
        if not hasattr(self, "cards"):
            return
        try:
            data = {
                "layout": self.layout_var.get(),
                "ports": [v.get().strip() for v in self.port_vars],
                "bauds": [int(v.get()) for v in self.baud_vars],
                "step_hz": (
                    self._pre_memory_step
                    if self._memory_armed and self._pre_memory_step in (1000, 500, 100, 10)
                    else self.step_var.get()
                ),
                "tx_source": self.tx_source_var.get(),
                "tx_action": self.tx_action_var.get(),
                "parking_frequency": self.parking_frequency_var.get(),
                "polling": self.polling_var.get(),
                "safety_acknowledged": self.safety_ack_var.get(),
                "skin": self._skin_preference,
                "display_scheme": self._display_scheme,
                "alignment_profiles": self._alignment_profiles,
                "drivers": [self._stored_driver_path(path) for path in self._driver_paths],
                "civ_addresses": [v.get().strip() for v in self.civ_vars],
                "driver_test_history": self._driver_test_history,
            }
            self._config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._ui_pump_id is not None:
            try:
                self.after_cancel(self._ui_pump_id)
            except tk.TclError:
                pass
            self._ui_pump_id = None
        self._restore_pre_memory_step()
        self._save_config()
        self._stop_coordinator()
        for index, engine in enumerate(self.engines):
            try:
                if self.radios[index] is not None:
                    self.radios[index].disable_auto_information()
            except Exception:
                pass
            engine.close()
        self.destroy()


if __name__ == "__main__":
    RigMirrorApp().mainloop()
