"""Configuration for Simple Solar Guider.

Provides a :class:`Config` dataclass holding all user-tunable settings plus
JSON load/save helpers. Both helpers are deliberately fault-tolerant: a missing
or corrupt config file yields defaults, unknown keys are ignored, and saving
never raises.
"""

from dataclasses import dataclass, asdict, field
import json
import os
import sys


@dataclass
class Config:
    """All persisted settings for the app, with safe defaults."""

    # --- image source ---
    source_type: str = "demo"        # "demo", "camera" or "folder"
    camera_index: int = 0
    sharpcap_folder: str = ""
    # --- mount serial ---
    com_port: str = ""
    baudrate: int = 9600
    # --- mount ASCOM (Windows) ---
    ascom_prog_id: str = ""   # ASCOM Telescope ProgID, im ASCOM-Chooser gewählt
    # --- detection ---
    threshold: int = 60              # 0-255 grayscale threshold
    min_radius: int = 20             # ignore contours smaller than this radius (px)
    # --- guiding ---
    deadband_px: int = 20
    max_pulse_ms: int = 300
    correction_interval: float = 1.0  # seconds between corrections
    manual_pulse_ms: int = 150       # Bewegungsdauer pro manuellem Klick (ms)
    invert_ra: bool = False
    invert_dec: bool = False
    # --- calibration (px moved per ms of pulse); 0 means "not calibrated" ---
    px_per_ms_ra: float = 0.0
    px_per_ms_dec: float = 0.0


# Directory that holds config.json. When running normally this is the folder of
# this module; when running as a PyInstaller-frozen .exe, __file__ points into a
# temporary extraction dir that is deleted on exit, so we use the folder of the
# .exe instead so settings persist next to the executable.
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Absolute path to "config.json".
CONFIG_PATH = os.path.join(_BASE_DIR, "config.json")


def load_config() -> Config:
    """Load configuration from :data:`CONFIG_PATH`.

    Returns a :class:`Config` populated from the JSON file. If the file is
    missing, unreadable, or contains corrupt JSON, defaults are returned.
    Unknown/extra keys in the file are ignored; only keys that exist on
    :class:`Config` are assigned. This function never raises.
    """
    cfg = Config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError) as exc:
        # Missing file or corrupt/unreadable JSON -> use defaults.
        print(f"[config] Using defaults ({exc})")
        return cfg

    if not isinstance(data, dict):
        print("[config] config.json is not a JSON object; using defaults")
        return cfg

    # Only assign keys that exist on the dataclass; ignore the rest.
    valid_keys = set(cfg.__dataclass_fields__)
    for key, value in data.items():
        if key in valid_keys:
            setattr(cfg, key, value)
    return cfg


def save_config(cfg: Config) -> None:
    """Write ``cfg`` as pretty-printed JSON to :data:`CONFIG_PATH`.

    Any failure (e.g. permission/IO error) is logged rather than raised, so a
    save attempt can never crash the application.
    """
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(asdict(cfg), fh, indent=4)
    except (OSError, TypeError) as exc:
        print(f"[config] WARNING: failed to save config: {exc}")
