"""camera.py — Pluggable image sources for the Simple Solar Guider.

Defines a single ``ImageSource`` interface and two concrete backends:

* ``OpenCVCamera``      — live frames from a ``cv2.VideoCapture`` device.
* ``FolderImageSource`` — the newest image file in a watched folder (e.g. the
                          directory SharpCap writes captures into).

``create_source(cfg)`` dispatches on ``cfg.source_type`` and never raises;
on bad input it returns a source whose ``is_opened()`` reports ``False``.

NOTE: This module must NOT be imported by config.py (would create a cycle).
``Config`` is referenced only for type hints, behind ``TYPE_CHECKING``.
"""

from __future__ import annotations

import glob
import os
from typing import TYPE_CHECKING, Optional

import cv2
import numpy as np

if TYPE_CHECKING:  # imported only for static type checking, never at runtime
    from config import Config

# Image extensions we attempt to load from a folder (FITS intentionally skipped).
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")


class ImageSource:
    """Base interface for all image sources.

    Subclasses return the latest frame as an OpenCV BGR ``np.ndarray`` (uint8)
    or ``None`` when no frame is available.
    """

    def get_frame(self) -> Optional[np.ndarray]:
        """Return the latest BGR frame, or ``None`` if unavailable."""
        raise NotImplementedError

    def release(self) -> None:
        """Release any underlying resources. Safe to call multiple times."""
        raise NotImplementedError

    def is_opened(self) -> bool:
        """Return ``True`` if the source is ready to produce frames."""
        raise NotImplementedError


class OpenCVCamera(ImageSource):
    """Live image source backed by ``cv2.VideoCapture``."""

    def __init__(self, index: int = 0):
        self.index = index
        try:
            self._cap: Optional[cv2.VideoCapture] = cv2.VideoCapture(index)
        except Exception as exc:  # be defensive — never raise from a source
            print(f"[OpenCVCamera] Failed to open camera {index}: {exc}")
            self._cap = None

    def get_frame(self) -> Optional[np.ndarray]:
        """Grab a single frame; returns ``None`` on any read failure."""
        if self._cap is None or not self._cap.isOpened():
            return None
        try:
            ok, frame = self._cap.read()
        except Exception as exc:
            print(f"[OpenCVCamera] read() error: {exc}")
            return None
        if not ok or frame is None:
            return None
        return frame

    def release(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()


class FolderImageSource(ImageSource):
    """Image source that serves the newest image file in a folder.

    Useful with capture software (e.g. SharpCap) that continually writes new
    frames to a directory. ``get_frame`` returns the most recently modified
    supported image, loaded via ``cv2.imread`` (always BGR).
    """

    def __init__(self, folder: str):
        self.folder = folder or ""

    def _newest_path(self) -> Optional[str]:
        """Return the path of the newest supported image, or ``None``."""
        if not self.folder or not os.path.isdir(self.folder):
            return None
        candidates: list[str] = []
        for ext in _IMAGE_EXTENSIONS:
            # Match both lower- and upper-case extensions.
            candidates.extend(glob.glob(os.path.join(self.folder, f"*{ext}")))
            candidates.extend(glob.glob(os.path.join(self.folder, f"*{ext.upper()}")))
        if not candidates:
            return None
        try:
            return max(candidates, key=os.path.getmtime)
        except (OSError, ValueError):
            # A file may vanish between glob and getmtime (writer races); bail.
            return None

    def get_frame(self) -> Optional[np.ndarray]:
        """Load and return the newest image in the folder, or ``None``."""
        path = self._newest_path()
        if path is None:
            return None
        try:
            frame = cv2.imread(path, cv2.IMREAD_COLOR)
        except Exception as exc:
            print(f"[FolderImageSource] imread error for {path}: {exc}")
            return None
        if frame is None:
            return None
        return frame

    def release(self) -> None:
        # Nothing to release for a folder-backed source.
        pass

    def is_opened(self) -> bool:
        return bool(self.folder) and os.path.isdir(self.folder)


def create_source(cfg: "Config") -> ImageSource:
    """Create an :class:`ImageSource` from a ``Config``.

    Dispatches on ``cfg.source_type``: ``"camera"`` -> :class:`OpenCVCamera`,
    anything else (default ``"folder"``) -> :class:`FolderImageSource`.

    Never raises. On malformed config it returns a source that reports
    ``is_opened() == False`` so callers can degrade gracefully.
    """
    try:
        source_type = getattr(cfg, "source_type", "folder")
        if source_type == "camera":
            return OpenCVCamera(getattr(cfg, "camera_index", 0))
        return FolderImageSource(getattr(cfg, "sharpcap_folder", ""))
    except Exception as exc:
        print(f"[create_source] Falling back to closed source: {exc}")
        # Guaranteed-closed fallback: an empty folder source.
        return FolderImageSource("")
