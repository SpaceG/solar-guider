"""gui.py — Einfache deutsche Oberfläche für den Sonnen-Guider (ZWO AM3).

Bewusst simpel gehalten: drei klare Schritte (Bild → Montierung → Guiding) und
ein großer Start-Knopf. Beim Start zeigt die App automatisch ein Demo-Testbild,
damit man die Sonnenerkennung sofort sieht – ganz ohne Kamera/Hardware.

Genutzte Module (gleiche Schnittstellen wie zuvor):
  config.py            – Einstellungen laden/speichern
  camera.py            – Bildquellen (Demo / Kamera / Ordner)
  image_processing.py  – Sonnenerkennung + Overlay
  mount_control.py     – serielle Montierungssteuerung (LX200)
"""

from __future__ import annotations

import logging
import time

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from camera import create_source
from config import Config, load_config, save_config
from image_processing import SunDetection, detect_sun, draw_overlay
from mount_control import ASCOMMount

# Versionsnummer der App (wird in der Fensterleiste und im Log angezeigt).
APP_VERSION = "0.9.1"

# Anzeigetext der Bildquellen-Auswahl <-> interner cfg.source_type-Wert.
_SOURCE_LABELS = [
    ("Demo (Testbild)", "demo"),
    ("Kamera", "camera"),
    ("Ordner (SharpCap)", "folder"),
]


def _opposite(direction: str) -> str:
    """Gegenrichtung: N<->S, E<->W."""
    return {"N": "S", "S": "N", "E": "W", "W": "E"}.get(direction, direction)


def _pulse_ms_for(px: float, px_per_ms: float, max_ms: int) -> int:
    """Pulslänge in ms für einen Drift von ``px`` Pixeln.

    Mit Kalibrierung proportional, sonst konservativer Festwert. Immer auf
    ``[0, max_ms]`` begrenzt.
    """
    if px_per_ms and px_per_ms > 0:
        return max(0, min(int(px / px_per_ms), max_ms))
    return min(150, max_ms)


class _LogHandler(logging.Handler):
    """Leitet Log-Meldungen (z. B. gesendete Mount-Befehle) ins Log-Fenster."""

    def __init__(self, append_fn):
        super().__init__()
        self._append = append_fn

    def emit(self, record):
        try:
            self._append(self.format(record))
        except Exception:
            pass


class MainWindow(QMainWindow):
    """Hauptfenster: links Livebild, rechts drei einfache Schritte."""

    def __init__(self):
        super().__init__()
        self.cfg: Config = load_config()
        self.source = None          # aktuelle Bildquelle (ImageSource) oder None
        self.mount = None           # ASCOMMount oder None
        self.guiding = False        # läuft Auto-Guiding gerade?
        self.last_correction = 0.0  # Zeitpunkt der letzten Korrektur (monotonic)
        self.last_detection: SunDetection | None = None
        # Halte-Position (x, y): wo die Sonne beim Guiding-Start war. Das Guiding
        # haelt DIESE Position, nicht die Bildmitte (wichtig fuer Rand/Protuberanz).
        self.guide_target = None
        # Vorherige Abweichung je Achse — fuer automatische Richtungs-Erkennung.
        self._prev_err_ra = None
        self._prev_err_dec = None

        self.setWindowTitle(f"Sonnen-Guider v{APP_VERSION} – ZWO AM3")
        self.resize(1000, 620)
        self.setMinimumSize(640, 400)  # passt auch auf kleinere Bildschirme

        self._build_ui()
        self._init_from_cfg()

        # Logger für Montierungsbefehle -> Log-Fenster.
        self.logger = logging.getLogger("solar_guider")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        h = _LogHandler(self.log_view.appendPlainText)
        h.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(h)

        # Aufnahmeschleife (~15 Bilder/s).
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        self.timer.setInterval(66)

        self._log(f"Sonnen-Guider v{APP_VERSION} bereit. Tipp: oben rechts auf "
                  "'Bild starten' klicken - es laeuft ein Demo-Testbild.")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # ---- links: Livebild ----
        self.image_label = QLabel("Kein Bild - auf 'Bild starten' klicken")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(400, 400)
        self.image_label.setStyleSheet(
            "background-color: #202020; color: #888; font-size: 14px;")
        root.addWidget(self.image_label, stretch=3)

        # ---- rechts: Bedienung (scrollbar, damit alles erreichbar ist) ----
        side_container = QWidget()
        side = QVBoxLayout(side_container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(side_container)
        scroll.setMinimumWidth(360)
        root.addWidget(scroll, stretch=2)

        intro = QLabel("So geht's:  1) Bild starten   2) Montierung verbinden   "
                       "3) Guiding starten")
        intro.setWordWrap(True)
        intro.setStyleSheet("font-weight: bold;")
        side.addWidget(intro)

        side.addWidget(self._build_image_box())
        side.addWidget(self._build_mount_box())
        side.addWidget(self._build_guide_box())
        side.addWidget(self._build_emergency_button())
        side.addWidget(self._build_advanced_box())

        log_box = QGroupBox("Meldungen")
        log_lay = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFixedHeight(110)
        log_lay.addWidget(self.log_view)
        side.addWidget(log_box)

        side.addStretch(1)

    def _build_image_box(self) -> QGroupBox:
        box = QGroupBox("1. Bild")
        lay = QGridLayout(box)

        lay.addWidget(QLabel("Quelle:"), 0, 0)
        self.source_combo = QComboBox()
        for label, _ in _SOURCE_LABELS:
            self.source_combo.addItem(label)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        lay.addWidget(self.source_combo, 0, 1, 1, 2)

        self.cam_label = QLabel("Kamera-Nr.:")
        lay.addWidget(self.cam_label, 1, 0)
        self.cam_index = QSpinBox()
        self.cam_index.setRange(0, 10)
        lay.addWidget(self.cam_index, 1, 1)

        self.browse_btn = QPushButton("Ordner waehlen ...")
        self.browse_btn.clicked.connect(self._browse_folder)
        lay.addWidget(self.browse_btn, 2, 0, 1, 2)
        self.folder_label = QLabel("(kein Ordner gewaehlt)")
        self.folder_label.setStyleSheet("color: #777;")
        self.folder_label.setWordWrap(True)
        lay.addWidget(self.folder_label, 3, 0, 1, 3)

        self.live_btn = QPushButton(">  Bild starten")
        self.live_btn.setMinimumHeight(34)
        self.live_btn.clicked.connect(self._toggle_live)
        lay.addWidget(self.live_btn, 4, 0, 1, 3)

        self.detect_label = QLabel("Status: -")
        lay.addWidget(self.detect_label, 5, 0, 1, 3)
        return box

    def _build_mount_box(self) -> QGroupBox:
        box = QGroupBox("2. Montierung (ZWO AM3 ueber ASCOM)")
        lay = QGridLayout(box)

        self.choose_btn = QPushButton("Montierung waehlen (ASCOM) ...")
        self.choose_btn.clicked.connect(self._choose_mount)
        lay.addWidget(self.choose_btn, 0, 0, 1, 3)

        self.ascom_label = QLabel("(keine Montierung gewaehlt)")
        self.ascom_label.setStyleSheet("color: #777;")
        self.ascom_label.setWordWrap(True)
        lay.addWidget(self.ascom_label, 1, 0, 1, 3)

        self.connect_btn = QPushButton("Verbinden")
        self.connect_btn.clicked.connect(self._connect_mount)
        lay.addWidget(self.connect_btn, 2, 0)
        self.disconnect_btn = QPushButton("Trennen")
        self.disconnect_btn.clicked.connect(self._disconnect_mount)
        lay.addWidget(self.disconnect_btn, 2, 1)

        self.mount_status = QLabel("Montierung: getrennt")
        self.mount_status.setStyleSheet("color: #b00;")
        lay.addWidget(self.mount_status, 3, 0, 1, 3)

        # Nachfuehrung (Tracking): haelt die Sonne grob; Basis fuer Guiding.
        self.tracking_btn = QPushButton("Nachfuehrung (Tracking) EIN")
        self.tracking_btn.clicked.connect(self._toggle_tracking)
        lay.addWidget(self.tracking_btn, 4, 0, 1, 3)
        self.tracking_status = QLabel("Nachfuehrung: aus")
        self.tracking_status.setStyleSheet("color: #777;")
        lay.addWidget(self.tracking_status, 5, 0, 1, 3)

        # Kleiner Bewegungstest (kurze, sichtbare Bewegung).
        lay.addWidget(QLabel("Bewegungstest:"), 6, 0, 1, 3)
        btn_row = QHBoxLayout()
        for text, direction in (("hoch N", "N"), ("runter S", "S"),
                                ("links O", "E"), ("rechts W", "W")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, d=direction: self._manual_pulse(d))
            btn_row.addWidget(b)
        stop_b = QPushButton("Stop")
        stop_b.clicked.connect(self._manual_stop)
        btn_row.addWidget(stop_b)
        wrap = QWidget()
        wrap.setLayout(btn_row)
        lay.addWidget(wrap, 7, 0, 1, 3)

        # Regler: wie weit sich die Montierung pro Klick bewegt.
        self.step_label = QLabel("Bewegung pro Klick: 150 ms")
        lay.addWidget(self.step_label, 8, 0, 1, 3)
        self.step_slider = QSlider(Qt.Orientation.Horizontal)
        self.step_slider.setRange(20, 1000)
        self.step_slider.setValue(150)
        self.step_slider.valueChanged.connect(self._on_step_changed)
        lay.addWidget(self.step_slider, 9, 0, 1, 3)
        return box

    def _build_guide_box(self) -> QGroupBox:
        box = QGroupBox("3. Guiding")
        lay = QVBoxLayout(box)

        info = QLabel("Haelt die Sonne dort, wo sie beim Start ist - auch am "
                      "Rand (z.B. fuer eine Protuberanz).")
        info.setWordWrap(True)
        lay.addWidget(info)

        self.guide_btn = QPushButton("GUIDING STARTEN")
        self.guide_btn.setMinimumHeight(48)
        self.guide_btn.setStyleSheet(
            "QPushButton { background-color: #1e8449; color: white; "
            "font-size: 16px; font-weight: bold; border-radius: 6px; }"
            "QPushButton:pressed { background-color: #166036; }")
        self.guide_btn.clicked.connect(self._toggle_guiding)
        lay.addWidget(self.guide_btn)

        self.calibrate_btn = QPushButton("Kalibrieren (optional)")
        self.calibrate_btn.clicked.connect(self._calibrate)
        lay.addWidget(self.calibrate_btn)
        self.calib_label = QLabel("RA: 0.000 px/ms   DEC: 0.000 px/ms")
        self.calib_label.setStyleSheet("color: #777;")
        lay.addWidget(self.calib_label)
        return box

    def _build_emergency_button(self) -> QPushButton:
        self.emergency_btn = QPushButton("NOT-AUS")
        self.emergency_btn.setMinimumHeight(46)
        self.emergency_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-size: 16px; font-weight: bold; border-radius: 6px; }"
            "QPushButton:pressed { background-color: #922b21; }")
        self.emergency_btn.clicked.connect(self._emergency_stop)
        return self.emergency_btn

    def _build_advanced_box(self) -> QGroupBox:
        box = QGroupBox("Erweiterte Einstellungen (fuer Fortgeschrittene)")
        box.setCheckable(True)
        box.setChecked(False)
        outer = QVBoxLayout(box)
        content = QWidget()
        outer.addWidget(content)
        lay = QGridLayout(content)

        row = 0
        lay.addWidget(QLabel("Totband (px):"), row, 0)
        self.deadband_spin = QSpinBox()
        self.deadband_spin.setRange(1, 500)
        lay.addWidget(self.deadband_spin, row, 1)

        row += 1
        lay.addWidget(QLabel("Max. Puls (ms):"), row, 0)
        self.maxpulse_spin = QSpinBox()
        self.maxpulse_spin.setRange(10, 1000)
        lay.addWidget(self.maxpulse_spin, row, 1)

        row += 1
        lay.addWidget(QLabel("Korrektur-Intervall (s):"), row, 0)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.2, 30.0)
        self.interval_spin.setSingleStep(0.5)
        lay.addWidget(self.interval_spin, row, 1)

        row += 1
        lay.addWidget(QLabel("Schwellwert (0-255):"), row, 0)
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(0, 255)
        lay.addWidget(self.threshold_spin, row, 1)

        row += 1
        lay.addWidget(QLabel("Min. Radius (px):"), row, 0)
        self.minradius_spin = QSpinBox()
        self.minradius_spin.setRange(1, 500)
        lay.addWidget(self.minradius_spin, row, 1)

        row += 1
        self.invert_ra_check = QCheckBox("RA umkehren (O<->W)")
        lay.addWidget(self.invert_ra_check, row, 0, 1, 2)
        row += 1
        self.invert_dec_check = QCheckBox("DEC umkehren (N<->S)")
        lay.addWidget(self.invert_dec_check, row, 0, 1, 2)

        row += 1
        save_btn = QPushButton("Einstellungen speichern")
        save_btn.clicked.connect(self._save_settings)
        lay.addWidget(save_btn, row, 0, 1, 2)

        # Aenderungen sofort in cfg uebernehmen.
        for w in (self.deadband_spin, self.maxpulse_spin,
                  self.threshold_spin, self.minradius_spin):
            w.valueChanged.connect(self._apply_settings)
        self.interval_spin.valueChanged.connect(self._apply_settings)
        self.invert_ra_check.toggled.connect(self._apply_settings)
        self.invert_dec_check.toggled.connect(self._apply_settings)

        box.toggled.connect(content.setVisible)
        content.setVisible(False)
        return box

    # -------------------------------------------------------------- cfg <-> UI
    def _init_from_cfg(self) -> None:
        cfg = self.cfg
        idx = next((i for i, (_, v) in enumerate(_SOURCE_LABELS)
                    if v == cfg.source_type), 0)
        self.source_combo.setCurrentIndex(idx)
        self.cam_index.setValue(cfg.camera_index)
        if cfg.sharpcap_folder:
            self.folder_label.setText(cfg.sharpcap_folder)
        if cfg.ascom_prog_id:
            self.ascom_label.setText(f"Gewaehlt: {cfg.ascom_prog_id}")
        self.deadband_spin.setValue(cfg.deadband_px)
        self.maxpulse_spin.setValue(cfg.max_pulse_ms)
        self.interval_spin.setValue(cfg.correction_interval)
        self.step_slider.setValue(cfg.manual_pulse_ms)
        self.step_label.setText(f"Bewegung pro Klick: {cfg.manual_pulse_ms} ms")
        self.threshold_spin.setValue(cfg.threshold)
        self.minradius_spin.setValue(cfg.min_radius)
        self.invert_ra_check.setChecked(cfg.invert_ra)
        self.invert_dec_check.setChecked(cfg.invert_dec)
        self.calib_label.setText(
            f"RA: {cfg.px_per_ms_ra:.3f} px/ms   DEC: {cfg.px_per_ms_dec:.3f} px/ms")
        self._on_source_changed()

    def _apply_settings(self) -> None:
        cfg = self.cfg
        cfg.source_type = _SOURCE_LABELS[self.source_combo.currentIndex()][1]
        cfg.camera_index = self.cam_index.value()
        cfg.deadband_px = self.deadband_spin.value()
        cfg.max_pulse_ms = self.maxpulse_spin.value()
        cfg.correction_interval = self.interval_spin.value()
        cfg.threshold = self.threshold_spin.value()
        cfg.min_radius = self.minradius_spin.value()
        cfg.invert_ra = self.invert_ra_check.isChecked()
        cfg.invert_dec = self.invert_dec_check.isChecked()

    def _save_settings(self) -> None:
        self._apply_settings()
        save_config(self.cfg)
        self._log("Einstellungen gespeichert.")

    # ------------------------------------------------------------- Bildquelle
    def _on_source_changed(self) -> None:
        stype = _SOURCE_LABELS[self.source_combo.currentIndex()][1]
        is_cam = stype == "camera"
        is_folder = stype == "folder"
        self.cam_label.setVisible(is_cam)
        self.cam_index.setVisible(is_cam)
        self.browse_btn.setVisible(is_folder)
        self.folder_label.setVisible(is_folder)
        self._apply_settings()

    def _browse_folder(self) -> None:
        start = self.cfg.sharpcap_folder or ""
        folder = QFileDialog.getExistingDirectory(self, "SharpCap-Ordner waehlen", start)
        if folder:
            self.cfg.sharpcap_folder = folder
            self.folder_label.setText(folder)
            self._log(f"Ordner: {folder}")

    def _toggle_live(self) -> None:
        if self.timer.isActive():
            self._stop_live()
        else:
            self._start_live()

    def _start_live(self) -> None:
        self._apply_settings()
        try:
            if self.source is not None:
                self.source.release()
            self.source = create_source(self.cfg)
        except Exception as exc:
            self._log(f"Bildquelle-Fehler: {exc}")
            return
        if not self.source.is_opened():
            self._log("Bildquelle nicht bereit (Kamera/Ordner pruefen). "
                      "Tipp: Quelle 'Demo (Testbild)' funktioniert immer.")
        self.timer.start()
        self.live_btn.setText("[] Bild stoppen")
        self._log("Bild laeuft.")

    def _stop_live(self) -> None:
        self.timer.stop()
        if self.guiding:
            self._set_guiding(False)
        if self.source is not None:
            self.source.release()
            self.source = None
        self.live_btn.setText(">  Bild starten")
        self.image_label.setText("Bild gestoppt")
        self.detect_label.setText("Status: -")
        self._log("Bild gestoppt.")

    # ------------------------------------------------------------- Montierung
    def _choose_mount(self) -> None:
        """ASCOM-Chooser oeffnen, damit der Nutzer die Montierung auswaehlt."""
        chooser_mount = ASCOMMount(self.cfg.ascom_prog_id, logger=self.logger)
        prog = chooser_mount.choose()
        if prog:
            self.cfg.ascom_prog_id = prog
            self.ascom_label.setText(f"Gewaehlt: {prog}")
            save_config(self.cfg)

    def _connect_mount(self) -> None:
        # Noch nichts gewaehlt? Dann zuerst den Chooser oeffnen.
        if not self.cfg.ascom_prog_id:
            self._choose_mount()
            if not self.cfg.ascom_prog_id:
                self._log("Bitte zuerst eine Montierung waehlen (ASCOM).")
                return
        try:
            self.mount = ASCOMMount(self.cfg.ascom_prog_id, logger=self.logger)
            ok = self.mount.connect()
        except Exception as exc:
            self._log(f"Verbindungsfehler: {exc}")
            ok = False
        if ok:
            self.mount_status.setText("Montierung: verbunden")
            self.mount_status.setStyleSheet("color: #0a0;")
            self._update_tracking_label()
        else:
            self.mount_status.setText("Montierung: Verbindung fehlgeschlagen")
            self.mount_status.setStyleSheet("color: #b00;")
            self._log("Verbindung fehlgeschlagen. Pruefe: AM3 an + per USB? "
                      "ASCOM + ZWO-Treiber installiert? WICHTIG: anderes "
                      "Steuerprogramm (z.B. SkyAtlas) vorher schliessen - die "
                      "Montierung erlaubt meist nur EINE Verbindung.")

    def _disconnect_mount(self) -> None:
        if self.guiding:
            self._set_guiding(False)
        if self.mount is not None:
            try:
                self.mount.stop()
                self.mount.disconnect()
            except Exception:
                pass
            self.mount = None
        self.mount_status.setText("Montierung: getrennt")
        self.mount_status.setStyleSheet("color: #b00;")
        self._update_tracking_label()
        self._log("Montierung getrennt.")

    def _mount_connected(self) -> bool:
        return self.mount is not None and self.mount.is_connected()

    def _toggle_tracking(self) -> None:
        """Nachfuehrung (Tracking) ein-/ausschalten."""
        if not self._mount_connected():
            self._log("Nachfuehrung: bitte zuerst Montierung verbinden.")
            return
        want_on = not (hasattr(self.mount, "is_tracking") and self.mount.is_tracking())
        if hasattr(self.mount, "set_tracking"):
            self.mount.set_tracking(want_on)
        self._update_tracking_label()

    def _update_tracking_label(self) -> None:
        on = (self.mount is not None and hasattr(self.mount, "is_tracking")
              and self.mount.is_tracking())
        if on:
            self.tracking_status.setText("Nachfuehrung: AN")
            self.tracking_status.setStyleSheet("color: #0a0;")
            self.tracking_btn.setText("Nachfuehrung (Tracking) AUS")
        else:
            self.tracking_status.setText("Nachfuehrung: aus")
            self.tracking_status.setStyleSheet("color: #777;")
            self.tracking_btn.setText("Nachfuehrung (Tracking) EIN")

    def _manual_pulse(self, direction: str) -> None:
        if not self._mount_connected():
            self._log("Bewegungstest: bitte zuerst Montierung verbinden.")
            return
        # Dauer der Bewegung pro Klick kommt vom Schieberegler (in ms).
        secs = max(0.02, self.cfg.manual_pulse_ms / 1000.0)
        try:
            if hasattr(self.mount, "slew_start"):
                self.mount.slew_start(direction)
                self._wait(secs)
                self.mount.slew_stop()
            else:
                self.mount.pulse(direction, self.cfg.manual_pulse_ms)
        except Exception as exc:
            self._log(f"Bewegungsfehler: {exc}")

    def _manual_stop(self) -> None:
        if self.mount is not None:
            try:
                if hasattr(self.mount, "slew_stop"):
                    self.mount.slew_stop()
                self.mount.stop()
            except Exception:
                pass

    def _on_step_changed(self, value: int) -> None:
        """Schieberegler 'Bewegung pro Klick' -> cfg.manual_pulse_ms."""
        self.cfg.manual_pulse_ms = int(value)
        self.step_label.setText(f"Bewegung pro Klick: {int(value)} ms")

    # ---------------------------------------------------------------- Guiding
    def _toggle_guiding(self) -> None:
        self._set_guiding(not self.guiding)

    def _set_guiding(self, on: bool) -> None:
        if on:
            if not self.timer.isActive():
                self._start_live()
            if not self._mount_connected():
                self._log("Guiding nicht gestartet: bitte zuerst Montierung "
                          "verbinden (Schritt 2).")
                return
            self._apply_settings()
            # Nachfuehrung sicherstellen (Basis fuer Guiding).
            if hasattr(self.mount, "set_tracking"):
                self.mount.set_tracking(True)
                self._update_tracking_label()
            self.guiding = True
            self.last_correction = 0.0
            # Aktuelle Sonnenposition als Halte-Ziel merken (Mitte ODER Rand).
            if self.last_detection is not None and self.last_detection.found:
                self.guide_target = self.last_detection.center
                self._log(f"Halte-Position gesetzt bei {self.guide_target}.")
            else:
                self.guide_target = None
                self._log("Halte-Position wird beim ersten erkannten Bild gesetzt.")
            self._prev_err_ra = None
            self._prev_err_dec = None
            self.guide_btn.setText("GUIDING STOPPEN")
            self.guide_btn.setStyleSheet(
                "QPushButton { background-color: #c0392b; color: white; "
                "font-size: 16px; font-weight: bold; border-radius: 6px; }"
                "QPushButton:pressed { background-color: #922b21; }")
            self._log("Guiding gestartet.")
        else:
            self.guiding = False
            self.guide_target = None
            self.guide_btn.setText("GUIDING STARTEN")
            self.guide_btn.setStyleSheet(
                "QPushButton { background-color: #1e8449; color: white; "
                "font-size: 16px; font-weight: bold; border-radius: 6px; }"
                "QPushButton:pressed { background-color: #166036; }")
            self._log("Guiding gestoppt.")

    def _do_guiding(self, det: SunDetection) -> None:
        # Sicherheit: nur bei aktivem Guiding, erkannter Sonne und Verbindung.
        if not (self.guiding and det.found and self._mount_connected()):
            return
        if det.center is None:
            return
        # Halte-Ziel = Position beim Start (nicht die Bildmitte!). So bleibt auch
        # eine Rand-/Protuberanz-Einstellung stehen.
        if self.guide_target is None:
            self.guide_target = det.center
            self._log(f"Halte-Position gesetzt bei {self.guide_target}.")
            return
        now = time.monotonic()
        if now - self.last_correction < self.cfg.correction_interval:
            return
        cfg = self.cfg
        # Abweichung von der Halte-Position.
        ex = det.center[0] - self.guide_target[0]
        ey = det.center[1] - self.guide_target[1]
        if abs(ex) <= cfg.deadband_px and abs(ey) <= cfg.deadband_px:
            self._prev_err_ra = None
            self._prev_err_dec = None
            return  # im Totband -> Ruhe lassen
        # Pro Zyklus die Achse mit der groesseren Abweichung korrigieren.
        if abs(ex) >= abs(ey):
            # Auto-Richtung: wenn die letzte RA-Korrektur den Fehler klar groesser
            # gemacht hat, war die Richtung verkehrt -> automatisch umdrehen.
            if (self._prev_err_ra is not None
                    and abs(ex) > abs(self._prev_err_ra) + 8):
                cfg.invert_ra = not cfg.invert_ra
                self.invert_ra_check.setChecked(cfg.invert_ra)
                self._log("Richtung RA automatisch umgedreht.")
            self._prev_err_ra = ex
            direction = "E" if ex > 0 else "W"
            if cfg.invert_ra:
                direction = _opposite(direction)
            error = ex
        else:
            if (self._prev_err_dec is not None
                    and abs(ey) > abs(self._prev_err_dec) + 8):
                cfg.invert_dec = not cfg.invert_dec
                self.invert_dec_check.setChecked(cfg.invert_dec)
                self._log("Richtung DEC automatisch umgedreht.")
            self._prev_err_dec = ey
            direction = "S" if ey > 0 else "N"
            if cfg.invert_dec:
                direction = _opposite(direction)
            error = ey
        self._guide_nudge(direction, error)
        self.last_correction = now

    def _guide_nudge(self, direction: str, error_px: float) -> None:
        """Korrektur per echter Bewegung (MoveAxis); Dauer ~ Abweichung.

        Deutlich staerker als ein Guide-Puls. Die Bewegung stoppt selbst nach
        ``ms`` (nicht blockierend ueber QTimer.singleShot).
        """
        ms = int(min(self.cfg.max_pulse_ms, max(40, abs(error_px) * 2)))
        try:
            if hasattr(self.mount, "slew_start"):
                self.mount.slew_start(direction)
                QTimer.singleShot(ms, self.mount.slew_stop)
            else:
                self.mount.pulse(direction, ms)
            self._log(f"Korrektur {direction} {ms} ms (Abw. {int(error_px)} px)")
        except Exception as exc:
            self._log(f"Guiding-Fehler: {exc}")

    def _calibrate(self) -> None:
        if not self._mount_connected():
            self._log("Kalibrierung: bitte zuerst Montierung verbinden.")
            return
        if self.source is None:
            self._log("Kalibrierung: bitte zuerst Bild starten.")
            return
        self._apply_settings()
        try:
            for axis, direction, attr in (("RA", "E", "px_per_ms_ra"),
                                          ("DEC", "N", "px_per_ms_dec")):
                d0 = detect_sun(self.source.get_frame(),
                                self.cfg.threshold, self.cfg.min_radius)
                if not d0.found or d0.center is None:
                    self._log(f"Kalibrierung {axis}: keine Sonne erkannt.")
                    continue
                ms = max(200, self.cfg.manual_pulse_ms)
                self.mount.pulse(direction, ms)
                self._wait(ms / 1000.0 + 0.6)
                d1 = detect_sun(self.source.get_frame(),
                                self.cfg.threshold, self.cfg.min_radius)
                if not d1.found or d1.center is None:
                    self._log(f"Kalibrierung {axis}: Sonne verloren.")
                    continue
                dist = ((d1.center[0] - d0.center[0]) ** 2
                        + (d1.center[1] - d0.center[1]) ** 2) ** 0.5
                val = round(dist / ms, 4) if ms > 0 else 0.0
                setattr(self.cfg, attr, val)
                self._log(f"Kalibrierung {axis}: {dist:.1f} px in {ms} ms "
                          f"-> {val:.4f} px/ms")
            self.calib_label.setText(
                f"RA: {self.cfg.px_per_ms_ra:.3f} px/ms   "
                f"DEC: {self.cfg.px_per_ms_dec:.3f} px/ms")
            save_config(self.cfg)
        except Exception as exc:
            self._log(f"Kalibrierungsfehler: {exc}")

    def _wait(self, seconds: float) -> None:
        """Kurze Wartepause, ohne die Oberflaeche einzufrieren."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    # --------------------------------------------------------------- Not-Aus
    def _emergency_stop(self) -> None:
        self._set_guiding(False)
        if self.mount is not None:
            try:
                self.mount.stop()
            except Exception:
                pass
        self._log("NOT-AUS ausgeloest - alle Bewegungen gestoppt.")

    # ---------------------------------------------------------- Aufnahmeschleife
    def _on_tick(self) -> None:
        try:
            if self.source is None:
                return
            frame = self.source.get_frame()
            if frame is None:
                self.detect_label.setText("Status: kein Bild")
                return
            det = detect_sun(frame, self.cfg.threshold, self.cfg.min_radius)
            self.last_detection = det
            overlay = draw_overlay(frame, det)
            self._show_image(overlay)
            if det.found:
                self.detect_label.setText(
                    f"Sonne erkannt   dx={det.dx:.0f}  dy={det.dy:.0f}  "
                    f"r={det.radius:.0f}")
            else:
                self.detect_label.setText(f"Status: {det.status}")
            if self.guiding:
                self._do_guiding(det)
        except Exception as exc:
            self._log(f"Bildverarbeitung-Fehler: {exc}")

    def _show_image(self, img: np.ndarray) -> None:
        if img is None:
            return
        if img.ndim == 2:  # Graustufen -> 3 Kanaele
            img = np.stack([img] * 3, axis=-1)
        h, w, ch = img.shape
        rgb = np.ascontiguousarray(img[:, :, ::-1])  # BGR -> RGB
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qimg.copy())
        self.image_label.setPixmap(pix.scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    # ------------------------------------------------------------------ Hilfe
    def _log(self, message: str) -> None:
        self.log_view.appendPlainText(message)
        print(f"[gui] {message}")

    def closeEvent(self, event):  # noqa: N802 (Qt-Signatur)
        try:
            if self.mount is not None:
                self.mount.stop()
                self.mount.disconnect()
            if self.source is not None:
                self.source.release()
        except Exception:
            pass
        super().closeEvent(event)
