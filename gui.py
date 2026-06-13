"""gui.py — PyQt6 user interface for the Simple Solar Guider.

Provides :class:`MainWindow`, the single top-level window. It wires together
the four backend modules (config, camera, image_processing, mount_control) into
a live capture/guide loop:

    left panel   : the live image with detection overlay (scaled QLabel)
    right panel  : source selection, live toggle, mount connection, manual
                   slew buttons, calibration, auto-guide, settings, EMERGENCY
                   STOP, and a read-only log view.

A ~15 fps QTimer drives the capture loop: grab a frame -> detect_sun ->
draw_overlay -> show -> update status -> (optionally) run one guiding step.

SAFETY: no motion is ever commanded unless Auto Guide is ON, the sun is
detected, AND the mount is connected. The capture tick is wrapped in
try/except so a single bad frame can never crash the UI.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# Plain module-name imports — main.py runs from inside the package dir so these
# resolve against sys.path[0]. Do NOT prefix with the package name.
from config import Config, load_config, save_config
from camera import create_source, ImageSource
from image_processing import detect_sun, draw_overlay, SunDetection
from mount_control import SerialMount, list_serial_ports


# --------------------------------------------------------------------------- #
# Pure guiding helpers (kept module-level per contract).
# --------------------------------------------------------------------------- #
def opposite(direction: str) -> str:
    """Return the opposite cardinal direction (E<->W, N<->S)."""
    return {"E": "W", "W": "E", "N": "S", "S": "N"}.get(direction, direction)


def _clamp(value: int, lo: int, hi: int) -> int:
    """Clamp ``value`` into the inclusive integer range [lo, hi]."""
    return max(lo, min(hi, value))


def pulse_ms_for(px: float, px_per_ms: float, max_ms: int) -> int:
    """Compute a guide-pulse length (ms) to correct ``px`` pixels of error.

    If calibrated (``px_per_ms`` > 0), scale the error by the calibration and
    clamp to ``[0, max_ms]``. When uncalibrated, fall back to a conservative
    fixed pulse of ``min(150, max_ms)``.
    """
    if px_per_ms and px_per_ms > 0:
        return _clamp(int(px / px_per_ms), 0, max_ms)
    return min(150, max_ms)


# --------------------------------------------------------------------------- #
# Logging bridge: route the mount logger into the GUI's text panel.
# --------------------------------------------------------------------------- #
class _QtLogHandler(logging.Handler):
    """A logging.Handler that appends formatted records to a QPlainTextEdit.

    The actual widget append is marshalled through a Qt signal so log records
    emitted from any thread are delivered safely on the GUI thread.
    """

    class _Emitter(QWidget):
        message = pyqtSignal(str)

    def __init__(self, text_widget: QPlainTextEdit):
        super().__init__()
        self._emitter = self._Emitter()
        self._emitter.message.connect(text_widget.appendPlainText)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emitter.message.emit(self.format(record))
        except Exception:
            # Logging must never crash the app.
            pass


# --------------------------------------------------------------------------- #
# Main window.
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    """Top-level window driving capture, detection, display, and guiding."""

    FPS = 15  # capture-loop target frame rate

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Simple Solar Guider")

        # --- runtime state -------------------------------------------------- #
        self.cfg: Config = load_config()
        self.source: Optional[ImageSource] = None
        self.mount: Optional[SerialMount] = None
        self.live: bool = False
        self.last_correction_time: float = 0.0
        self.last_detection: Optional[SunDetection] = None
        # Per-module logger surfaced in the GUI log panel.
        self.logger = logging.getLogger("solar_guider")
        self.logger.setLevel(logging.INFO)

        # --- build UI ------------------------------------------------------- #
        self._build_ui()
        self._attach_logging()
        self._populate_from_config()

        # --- capture timer -------------------------------------------------- #
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.setInterval(int(1000 / self.FPS))

        self._log("Solar Guider ready. Configure a source and press Start Live.")

    # ------------------------------------------------------------------ UI -- #
    def _build_ui(self) -> None:
        """Construct and lay out all widgets."""
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # ---------------- LEFT: live image ---------------- #
        self.image_label = QLabel("No image")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(640, 480)
        self.image_label.setFrameShape(QFrame.Shape.Box)
        self.image_label.setStyleSheet("background-color: #202020; color: #888;")
        root.addWidget(self.image_label, stretch=3)

        # ---------------- RIGHT: controls ---------------- #
        right = QVBoxLayout()
        right.addWidget(self._build_source_group())
        right.addWidget(self._build_mount_group())
        right.addWidget(self._build_manual_group())
        right.addWidget(self._build_guide_group())
        right.addWidget(self._build_settings_group())
        right.addWidget(self._build_emergency_button())
        right.addWidget(self._build_log_group(), stretch=1)

        right_container = QWidget()
        right_container.setLayout(right)
        right_container.setMinimumWidth(360)
        root.addWidget(right_container, stretch=1)

    def _build_source_group(self) -> QGroupBox:
        box = QGroupBox("Image Source")
        lay = QGridLayout(box)

        self.source_combo = QComboBox()
        self.source_combo.addItems(["Folder", "Camera"])
        lay.addWidget(QLabel("Source:"), 0, 0)
        lay.addWidget(self.source_combo, 0, 1)

        self.camera_index_spin = QSpinBox()
        self.camera_index_spin.setRange(0, 16)
        lay.addWidget(QLabel("Camera index:"), 1, 0)
        lay.addWidget(self.camera_index_spin, 1, 1)

        self.browse_btn = QPushButton("Browse SharpCap folder")
        self.browse_btn.clicked.connect(self._on_browse_folder)
        lay.addWidget(self.browse_btn, 2, 0, 1, 2)

        self.folder_label = QLabel("(no folder selected)")
        self.folder_label.setWordWrap(True)
        self.folder_label.setStyleSheet("color: #555;")
        lay.addWidget(self.folder_label, 3, 0, 1, 2)

        self.live_btn = QPushButton("Start Live")
        self.live_btn.setCheckable(True)
        self.live_btn.clicked.connect(self._on_toggle_live)
        lay.addWidget(self.live_btn, 4, 0, 1, 2)

        return box

    def _build_mount_group(self) -> QGroupBox:
        box = QGroupBox("Mount (Serial)")
        lay = QGridLayout(box)

        self.port_combo = QComboBox()
        lay.addWidget(QLabel("COM port:"), 0, 0)
        lay.addWidget(self.port_combo, 0, 1)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_ports)
        lay.addWidget(self.refresh_btn, 0, 2)

        self.baud_edit = QLineEdit("9600")
        lay.addWidget(QLabel("Baudrate:"), 1, 0)
        lay.addWidget(self.baud_edit, 1, 1, 1, 2)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect)
        lay.addWidget(self.connect_btn, 2, 0, 1, 2)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        lay.addWidget(self.disconnect_btn, 2, 2)

        self.mount_status_label = QLabel("Mount: disconnected")
        self.mount_status_label.setStyleSheet("color: #b00;")
        lay.addWidget(self.mount_status_label, 3, 0, 1, 3)

        self._refresh_ports()
        return box

    def _build_manual_group(self) -> QGroupBox:
        box = QGroupBox("Manual Slew (guide pulses)")
        lay = QGridLayout(box)

        self.north_btn = QPushButton("North")
        self.south_btn = QPushButton("South")
        self.east_btn = QPushButton("East")
        self.west_btn = QPushButton("West")
        self.stop_btn = QPushButton("Stop")

        self.north_btn.clicked.connect(lambda: self._manual_pulse("N"))
        self.south_btn.clicked.connect(lambda: self._manual_pulse("S"))
        self.east_btn.clicked.connect(lambda: self._manual_pulse("E"))
        self.west_btn.clicked.connect(lambda: self._manual_pulse("W"))
        self.stop_btn.clicked.connect(self._on_stop)

        # Cross layout: N on top, W/Stop/E in the middle, S on the bottom.
        lay.addWidget(self.north_btn, 0, 1)
        lay.addWidget(self.west_btn, 1, 0)
        lay.addWidget(self.stop_btn, 1, 1)
        lay.addWidget(self.east_btn, 1, 2)
        lay.addWidget(self.south_btn, 2, 1)
        return box

    def _build_guide_group(self) -> QGroupBox:
        box = QGroupBox("Auto Guide")
        lay = QVBoxLayout(box)

        self.auto_guide_check = QCheckBox("Enable Auto Guide")
        self.auto_guide_check.setChecked(False)  # DEFAULT UNCHECKED (safety)
        lay.addWidget(self.auto_guide_check)

        self.calibrate_btn = QPushButton("Calibrate")
        self.calibrate_btn.clicked.connect(self._on_calibrate)
        lay.addWidget(self.calibrate_btn)

        self.calib_label = QLabel("RA: 0.000 px/ms   DEC: 0.000 px/ms")
        self.calib_label.setStyleSheet("color: #555;")
        lay.addWidget(self.calib_label)
        return box

    def _build_settings_group(self) -> QGroupBox:
        box = QGroupBox("Settings")
        lay = QGridLayout(box)
        row = 0

        self.deadband_spin = QSpinBox()
        self.deadband_spin.setRange(0, 2000)
        lay.addWidget(QLabel("Deadband (px):"), row, 0)
        lay.addWidget(self.deadband_spin, row, 1)
        row += 1

        self.max_pulse_spin = QSpinBox()
        self.max_pulse_spin.setRange(0, 5000)
        lay.addWidget(QLabel("Max pulse (ms):"), row, 0)
        lay.addWidget(self.max_pulse_spin, row, 1)
        row += 1

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.0, 60.0)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setDecimals(2)
        lay.addWidget(QLabel("Correction interval (s):"), row, 0)
        lay.addWidget(self.interval_spin, row, 1)
        row += 1

        self.manual_pulse_spin = QSpinBox()
        self.manual_pulse_spin.setRange(0, 5000)
        lay.addWidget(QLabel("Manual pulse (ms):"), row, 0)
        lay.addWidget(self.manual_pulse_spin, row, 1)
        row += 1

        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(0, 255)
        lay.addWidget(QLabel("Threshold (0-255):"), row, 0)
        lay.addWidget(self.threshold_spin, row, 1)
        row += 1

        self.min_radius_spin = QSpinBox()
        self.min_radius_spin.setRange(0, 5000)
        lay.addWidget(QLabel("Min radius (px):"), row, 0)
        lay.addWidget(self.min_radius_spin, row, 1)
        row += 1

        self.invert_ra_check = QCheckBox("Invert RA (E<->W)")
        lay.addWidget(self.invert_ra_check, row, 0, 1, 2)
        row += 1

        self.invert_dec_check = QCheckBox("Invert DEC (N<->S)")
        lay.addWidget(self.invert_dec_check, row, 0, 1, 2)
        row += 1

        self.save_btn = QPushButton("Save settings")
        self.save_btn.clicked.connect(self._on_save_settings)
        lay.addWidget(self.save_btn, row, 0, 1, 2)
        return box

    def _build_emergency_button(self) -> QPushButton:
        self.emergency_btn = QPushButton("EMERGENCY STOP")
        self.emergency_btn.setMinimumHeight(56)
        self.emergency_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-size: 18px; font-weight: bold; border-radius: 6px; }"
            "QPushButton:pressed { background-color: #922b21; }"
        )
        self.emergency_btn.clicked.connect(self._on_emergency_stop)
        return self.emergency_btn

    def _build_log_group(self) -> QGroupBox:
        box = QGroupBox("Log")
        lay = QVBoxLayout(box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)  # cap memory growth
        lay.addWidget(self.log_view)
        return box

    # -------------------------------------------------------------- logging -- #
    def _attach_logging(self) -> None:
        """Route the GUI logger into the on-screen log panel."""
        handler = _QtLogHandler(self.log_view)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        self.logger.addHandler(handler)
        self._log_handler = handler  # keep a ref so it isn't GC'd

    def _log(self, message: str) -> None:
        """Convenience: log an INFO message (shown in the GUI panel)."""
        self.logger.info(message)

    # ----------------------------------------------------- config <-> widgets #
    def _populate_from_config(self) -> None:
        """Initialise all widgets from ``self.cfg``."""
        cfg = self.cfg
        self.source_combo.setCurrentText(
            "Camera" if cfg.source_type == "camera" else "Folder"
        )
        self.camera_index_spin.setValue(int(cfg.camera_index))
        self.folder_label.setText(cfg.sharpcap_folder or "(no folder selected)")
        self.baud_edit.setText(str(cfg.baudrate))

        self.deadband_spin.setValue(int(cfg.deadband_px))
        self.max_pulse_spin.setValue(int(cfg.max_pulse_ms))
        self.interval_spin.setValue(float(cfg.correction_interval))
        self.manual_pulse_spin.setValue(int(cfg.manual_pulse_ms))
        self.threshold_spin.setValue(int(cfg.threshold))
        self.min_radius_spin.setValue(int(cfg.min_radius))
        self.invert_ra_check.setChecked(bool(cfg.invert_ra))
        self.invert_dec_check.setChecked(bool(cfg.invert_dec))

        # Pre-select a saved COM port if present in the current list.
        if cfg.com_port:
            idx = self.port_combo.findText(cfg.com_port)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
            else:
                self.port_combo.addItem(cfg.com_port)
                self.port_combo.setCurrentText(cfg.com_port)

        self._update_calib_label()

    def _write_widgets_to_config(self) -> None:
        """Copy current widget values back into ``self.cfg``."""
        cfg = self.cfg
        cfg.source_type = "camera" if self.source_combo.currentText() == "Camera" else "folder"
        cfg.camera_index = int(self.camera_index_spin.value())
        # folder + com_port handled at their event sites; mirror current state.
        cfg.com_port = self.port_combo.currentText()
        try:
            cfg.baudrate = int(self.baud_edit.text())
        except (TypeError, ValueError):
            cfg.baudrate = 9600

        cfg.deadband_px = int(self.deadband_spin.value())
        cfg.max_pulse_ms = int(self.max_pulse_spin.value())
        cfg.correction_interval = float(self.interval_spin.value())
        cfg.manual_pulse_ms = int(self.manual_pulse_spin.value())
        cfg.threshold = int(self.threshold_spin.value())
        cfg.min_radius = int(self.min_radius_spin.value())
        cfg.invert_ra = bool(self.invert_ra_check.isChecked())
        cfg.invert_dec = bool(self.invert_dec_check.isChecked())

    def _update_calib_label(self) -> None:
        self.calib_label.setText(
            "RA: {:.3f} px/ms   DEC: {:.3f} px/ms".format(
                self.cfg.px_per_ms_ra, self.cfg.px_per_ms_dec
            )
        )

    # ----------------------------------------------------------- source I/O -- #
    def _on_browse_folder(self) -> None:
        """Choose the SharpCap capture folder."""
        start = self.cfg.sharpcap_folder or ""
        folder = QFileDialog.getExistingDirectory(self, "Select SharpCap folder", start)
        if folder:
            self.cfg.sharpcap_folder = folder
            self.folder_label.setText(folder)
            self._log(f"SharpCap folder set: {folder}")

    def _on_toggle_live(self) -> None:
        """Start or stop the capture loop."""
        if self.live_btn.isChecked():
            self._start_live()
        else:
            self._stop_live()

    def _start_live(self) -> None:
        self._write_widgets_to_config()
        # (Re)create the source from current config.
        if self.source is not None:
            try:
                self.source.release()
            except Exception:
                pass
        self.source = create_source(self.cfg)
        if not self.source.is_opened():
            self._log("WARNING: image source is not available — check folder/camera.")
        self.live = True
        self.live_btn.setText("Stop Live")
        self.live_btn.setChecked(True)
        self.timer.start()
        self._log("Live capture started.")

    def _stop_live(self) -> None:
        self.live = False
        self.timer.stop()
        self.live_btn.setText("Start Live")
        self.live_btn.setChecked(False)
        if self.source is not None:
            try:
                self.source.release()
            except Exception:
                pass
        self._log("Live capture stopped.")

    # ------------------------------------------------------------- mount I/O -- #
    def _refresh_ports(self) -> None:
        """Repopulate the COM-port combo from the OS."""
        current = self.port_combo.currentText()
        self.port_combo.clear()
        try:
            ports = list_serial_ports()
        except Exception as exc:
            ports = []
            self._log(f"WARNING: could not list serial ports: {exc}")
        self.port_combo.addItems(ports)
        if current:
            idx = self.port_combo.findText(current)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
            elif current:
                self.port_combo.addItem(current)
                self.port_combo.setCurrentText(current)

    def _on_connect(self) -> None:
        """Open the serial mount with the selected port/baud."""
        port = self.port_combo.currentText().strip()
        if not port:
            self._log("WARNING: no COM port selected.")
            return
        try:
            baud = int(self.baud_edit.text())
        except (TypeError, ValueError):
            baud = 9600
            self.baud_edit.setText("9600")

        self.cfg.com_port = port
        self.cfg.baudrate = baud

        # Tear down any prior connection first.
        self._on_disconnect()
        self.mount = SerialMount(port, baud, logger=self.logger)
        ok = self.mount.connect()
        self._update_mount_status()
        if ok:
            self._log(f"Mount connected on {port} @ {baud} baud.")
        else:
            self._log(f"WARNING: failed to connect to mount on {port}.")

    def _on_disconnect(self) -> None:
        """Disconnect the serial mount if connected."""
        if self.mount is not None:
            try:
                self.mount.disconnect()
            except Exception as exc:
                self._log(f"WARNING: error during disconnect: {exc}")
            self.mount = None
        self._update_mount_status()

    def _update_mount_status(self) -> None:
        connected = self.mount is not None and self.mount.is_connected()
        if connected:
            self.mount_status_label.setText(f"Mount: connected ({self.cfg.com_port})")
            self.mount_status_label.setStyleSheet("color: #0a0;")
        else:
            self.mount_status_label.setText("Mount: disconnected")
            self.mount_status_label.setStyleSheet("color: #b00;")

    def _mount_connected(self) -> bool:
        return self.mount is not None and self.mount.is_connected()

    # ----------------------------------------------------------- manual slew -- #
    def _manual_pulse(self, direction: str) -> None:
        """Issue a manual guide pulse in ``direction`` (N/S/E/W)."""
        if not self._mount_connected():
            self._log("Manual pulse ignored: mount not connected.")
            return
        ms = int(self.manual_pulse_spin.value())
        self.cfg.manual_pulse_ms = ms
        try:
            self.mount.pulse(direction, ms)
        except Exception as exc:
            self._log(f"WARNING: manual pulse failed: {exc}")

    def _on_stop(self) -> None:
        """Stop all mount motion (manual Stop button)."""
        if self.mount is not None:
            try:
                self.mount.stop()
            except Exception as exc:
                self._log(f"WARNING: stop failed: {exc}")

    def _on_emergency_stop(self) -> None:
        """EMERGENCY STOP: halt motion AND disable Auto Guide."""
        self.auto_guide_check.setChecked(False)
        if self.mount is not None:
            try:
                self.mount.stop()
            except Exception as exc:
                self._log(f"WARNING: emergency stop send failed: {exc}")
        self._log("EMERGENCY STOP triggered — Auto Guide disabled.")

    # ----------------------------------------------------------- calibration -- #
    def _on_calibrate(self) -> None:
        """Basic calibration: pulse E then N a known duration, measure px/ms.

        Measures the sun-center displacement between frames taken before and
        after each pulse and stores the result into the config. Intentionally
        simple — it assumes a stable sun and short settling time.
        """
        if not self._mount_connected():
            self._log("Calibration aborted: mount not connected.")
            return
        if not self.live or self.source is None:
            self._log("Calibration aborted: start Live capture first.")
            return

        calib_ms = 500  # fixed calibration pulse length

        # --- RA axis (East) --- #
        before = self._current_center()
        if before is None:
            self._log("Calibration aborted: sun not detected before RA pulse.")
            return
        try:
            self.mount.pulse("E", calib_ms)
        except Exception as exc:
            self._log(f"Calibration RA pulse failed: {exc}")
            return
        self._settle(calib_ms)
        after = self._current_center()
        if after is None:
            self._log("Calibration: sun lost after RA pulse; RA not calibrated.")
        else:
            dist = float(np.hypot(after[0] - before[0], after[1] - before[1]))
            self.cfg.px_per_ms_ra = (dist / calib_ms) if calib_ms else 0.0
            self._log(f"Calibrated RA: {self.cfg.px_per_ms_ra:.4f} px/ms ({dist:.1f} px).")

        # --- DEC axis (North) --- #
        before = self._current_center()
        if before is None:
            self._log("Calibration: sun not detected before DEC pulse.")
            self._update_calib_label()
            return
        try:
            self.mount.pulse("N", calib_ms)
        except Exception as exc:
            self._log(f"Calibration DEC pulse failed: {exc}")
            self._update_calib_label()
            return
        self._settle(calib_ms)
        after = self._current_center()
        if after is None:
            self._log("Calibration: sun lost after DEC pulse; DEC not calibrated.")
        else:
            dist = float(np.hypot(after[0] - before[0], after[1] - before[1]))
            self.cfg.px_per_ms_dec = (dist / calib_ms) if calib_ms else 0.0
            self._log(f"Calibrated DEC: {self.cfg.px_per_ms_dec:.4f} px/ms ({dist:.1f} px).")

        self._update_calib_label()

    def _current_center(self) -> Optional[tuple]:
        """Grab a frame, detect the sun, and return its (x, y) center or None."""
        if self.source is None:
            return None
        frame = self.source.get_frame()
        if frame is None:
            return None
        det = detect_sun(frame, int(self.threshold_spin.value()),
                         int(self.min_radius_spin.value()))
        if det.found and det.center is not None:
            return det.center
        return None

    def _settle(self, pulse_ms: int) -> None:
        """Wait for a pulse to complete plus a short settle window.

        Uses a blocking sleep only during the (manual) calibration step — the
        capture timer is single-shot driven, so this does not affect the live
        loop's safety guarantees.
        """
        time.sleep((pulse_ms / 1000.0) + 0.3)

    # -------------------------------------------------------------- settings -- #
    def _on_save_settings(self) -> None:
        """Persist current widget state to config.json."""
        self._write_widgets_to_config()
        save_config(self.cfg)
        self._log("Settings saved.")

    # ----------------------------------------------------------- capture loop #
    def _tick(self) -> None:
        """One capture/display/guide iteration. Never propagates exceptions."""
        try:
            if self.source is None:
                return
            frame = self.source.get_frame()
            if frame is None:
                self._set_image_message("No frame")
                return

            detection = detect_sun(
                frame,
                int(self.threshold_spin.value()),
                int(self.min_radius_spin.value()),
            )
            self.last_detection = detection

            overlay = draw_overlay(frame, detection)
            self._show_frame(overlay)
            self._update_status(detection)

            # Guiding step — gated by every safety rule inside.
            if self.auto_guide_check.isChecked():
                self._guiding_step(detection)
        except Exception as exc:
            # A single bad frame must never crash the UI.
            self._log(f"Tick error: {exc}")

    def _guiding_step(self, detection: SunDetection) -> None:
        """Apply one correction pulse per axis if error exceeds the deadband.

        SAFETY: returns immediately unless Auto Guide is on, the sun is found,
        and the mount is connected. Never moves when the sun is not found.
        """
        # Re-check all safety preconditions (defensive; caller also checks).
        if not self.auto_guide_check.isChecked():
            return
        if detection is None or not detection.found:
            return
        if not self._mount_connected():
            return

        now = time.monotonic()
        cfg = self.cfg
        # Keep config in sync with live settings widgets.
        cfg.deadband_px = int(self.deadband_spin.value())
        cfg.max_pulse_ms = int(self.max_pulse_spin.value())
        cfg.correction_interval = float(self.interval_spin.value())
        cfg.invert_ra = bool(self.invert_ra_check.isChecked())
        cfg.invert_dec = bool(self.invert_dec_check.isChecked())

        if (now - self.last_correction_time) < cfg.correction_interval:
            return

        dx, dy = detection.dx, detection.dy

        # --- RA axis --- #
        if abs(dx) > cfg.deadband_px:
            ra_dir = "E" if dx > 0 else "W"
            if cfg.invert_ra:
                ra_dir = opposite(ra_dir)
            ms = pulse_ms_for(abs(dx), cfg.px_per_ms_ra, cfg.max_pulse_ms)
            try:
                self.mount.pulse(ra_dir, ms)
            except Exception as exc:
                self._log(f"WARNING: RA guide pulse failed: {exc}")

        # --- DEC axis --- #
        if abs(dy) > cfg.deadband_px:
            dec_dir = "S" if dy > 0 else "N"
            if cfg.invert_dec:
                dec_dir = opposite(dec_dir)
            ms = pulse_ms_for(abs(dy), cfg.px_per_ms_dec, cfg.max_pulse_ms)
            try:
                self.mount.pulse(dec_dir, ms)
            except Exception as exc:
                self._log(f"WARNING: DEC guide pulse failed: {exc}")

        self.last_correction_time = now

    # ------------------------------------------------------------- rendering -- #
    def _show_frame(self, bgr: np.ndarray) -> None:
        """Convert a BGR ndarray to a scaled QPixmap and display it."""
        pix = self._bgr_to_pixmap(bgr)
        if pix is None:
            return
        scaled = pix.scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    @staticmethod
    def _bgr_to_pixmap(bgr: np.ndarray) -> Optional[QPixmap]:
        """Convert an OpenCV BGR uint8 ndarray to a QPixmap (RGB888)."""
        if bgr is None or bgr.size == 0:
            return None
        try:
            # Ensure 3-channel BGR uint8.
            if bgr.ndim == 2:
                arr = np.stack([bgr] * 3, axis=-1)
            else:
                arr = bgr
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)

            # BGR -> RGB without OpenCV (avoid an extra cv2 import here).
            rgb = np.ascontiguousarray(arr[:, :, ::-1])
            h, w, ch = rgb.shape
            bytes_per_line = ch * w
            image = QImage(
                rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888
            )
            # .copy() detaches the QImage from the numpy buffer's lifetime.
            return QPixmap.fromImage(image.copy())
        except Exception:
            return None

    def _set_image_message(self, text: str) -> None:
        self.image_label.setPixmap(QPixmap())  # clear
        self.image_label.setText(text)

    def _update_status(self, detection: SunDetection) -> None:
        """Refresh status indicators (mount status is updated on connect)."""
        self._update_mount_status()
        # The overlay already shows dx/dy/radius/status; nothing else required
        # here, but keep the hook for future status widgets.

    # ----------------------------------------------------------- lifecycle --- #
    def closeEvent(self, event) -> None:
        """Ensure motion is stopped and resources released on close."""
        try:
            self.timer.stop()
        except Exception:
            pass
        try:
            if self.mount is not None and self.mount.is_connected():
                self.mount.stop()
                self.mount.disconnect()
        except Exception:
            pass
        try:
            if self.source is not None:
                self.source.release()
        except Exception:
            pass
        super().closeEvent(event)
