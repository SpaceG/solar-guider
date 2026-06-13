"""Sun detection and overlay drawing for the Simple Solar Guider.

Public API:
    SunDetection  -- dataclass describing a detection result
    detect_sun    -- locate the solar disk in a BGR frame
    draw_overlay  -- render a visual overlay onto a copy of a frame
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np


@dataclass
class SunDetection:
    """Result of a single sun-detection pass on one frame."""

    found: bool
    center: Optional[Tuple[int, int]]   # (x, y) in px, or None
    radius: float                        # px, 0.0 if not found
    dx: float                            # center_x - image_center_x (0.0 if not found)
    dy: float                            # center_y - image_center_y (0.0 if not found)
    image_center: Tuple[int, int]        # (cx, cy) of the frame, always set
    status: str                          # short human status, e.g. "OK", "No sun"


def _image_center(frame: np.ndarray) -> Tuple[int, int]:
    """Return the integer (cx, cy) center of a frame from its shape."""
    h, w = frame.shape[:2]
    return (w // 2, h // 2)


def detect_sun(frame: np.ndarray, threshold: int, min_radius: int = 20) -> SunDetection:
    """Detect the solar disk in a BGR frame.

    Pipeline: grayscale -> light GaussianBlur -> binary threshold ->
    findContours -> largest contour by area -> minEnclosingCircle. Contours
    whose enclosing radius is below ``min_radius`` are rejected.

    Never raises. Returns a SunDetection with ``found=False`` and a sensible
    status string when the frame is empty/None or no valid sun is present.
    """
    # Guard against a missing/empty frame.
    if frame is None or not hasattr(frame, "size") or frame.size == 0:
        return SunDetection(
            found=False,
            center=None,
            radius=0.0,
            dx=0.0,
            dy=0.0,
            image_center=(0, 0),
            status="No frame",
        )

    try:
        cx, cy = _image_center(frame)

        # Convert to grayscale (handle already-grayscale frames).
        if frame.ndim == 3 and frame.shape[2] >= 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame if frame.ndim == 2 else frame[:, :, 0]

        # Light blur to suppress sensor noise before thresholding.
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # Binary threshold: bright solar disk -> white blob.
        thresh_val = int(max(0, min(255, threshold)))
        _, binary = cv2.threshold(blurred, thresh_val, 255, cv2.THRESH_BINARY)

        # Find external contours of bright regions.
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        if not contours:
            return SunDetection(
                found=False,
                center=None,
                radius=0.0,
                dx=0.0,
                dy=0.0,
                image_center=(cx, cy),
                status="No sun",
            )

        # Pick the largest contour by area (the solar disk).
        largest = max(contours, key=cv2.contourArea)
        (mx, my), radius = cv2.minEnclosingCircle(largest)
        radius = float(radius)
        center = (int(round(mx)), int(round(my)))

        # Reject blobs that are too small to be the sun.
        if radius < float(min_radius):
            return SunDetection(
                found=False,
                center=None,
                radius=0.0,
                dx=0.0,
                dy=0.0,
                image_center=(cx, cy),
                status="Too small",
            )

        dx = float(center[0] - cx)
        dy = float(center[1] - cy)

        return SunDetection(
            found=True,
            center=center,
            radius=radius,
            dx=dx,
            dy=dy,
            image_center=(cx, cy),
            status="OK",
        )
    except Exception as exc:  # never raise to the caller
        # Best-effort image center for diagnostics; fall back to (0, 0).
        try:
            ic = _image_center(frame)
        except Exception:
            ic = (0, 0)
        return SunDetection(
            found=False,
            center=None,
            radius=0.0,
            dx=0.0,
            dy=0.0,
            image_center=ic,
            status="Error: {}".format(exc),
        )


def draw_overlay(frame: np.ndarray, detection: SunDetection) -> np.ndarray:
    """Return a NEW BGR image with the detection overlay drawn on it.

    Draws a cyan crosshair at the image center, and (when found) a green
    circle of the detected radius, a red dot at the sun center, and a faint
    yellow line from center to sun. Four text lines (dx, dy, radius, status)
    are rendered top-left. Never raises; grayscale frames are converted to BGR.
    """
    # BGR color constants.
    CYAN = (255, 255, 0)
    RED = (0, 0, 255)
    GREEN = (0, 255, 0)
    YELLOW = (0, 255, 255)
    WHITE = (255, 255, 255)

    # Produce a BGR copy we can safely draw on.
    try:
        if frame is None or not hasattr(frame, "size") or frame.size == 0:
            # Nothing to draw on: return a small black canvas.
            return np.zeros((480, 640, 3), dtype=np.uint8)

        if frame.ndim == 2:
            canvas = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.ndim == 3 and frame.shape[2] == 1:
            canvas = cv2.cvtColor(frame[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif frame.ndim == 3 and frame.shape[2] == 4:
            canvas = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        else:
            canvas = frame.copy()

        # Ensure contiguous uint8 BGR for OpenCV drawing.
        if canvas.dtype != np.uint8:
            canvas = np.clip(canvas, 0, 255).astype(np.uint8)
        canvas = np.ascontiguousarray(canvas)
    except Exception:
        return np.zeros((480, 640, 3), dtype=np.uint8)

    try:
        ic = detection.image_center if detection is not None else None
        if not ic:
            ic = _image_center(canvas)
        icx, icy = int(ic[0]), int(ic[1])

        # Crosshair at image center (cyan).
        arm = 15
        cv2.line(canvas, (icx - arm, icy), (icx + arm, icy), CYAN, 1, cv2.LINE_AA)
        cv2.line(canvas, (icx, icy - arm), (icx, icy + arm), CYAN, 1, cv2.LINE_AA)

        if detection is not None and detection.found and detection.center is not None:
            scx, scy = int(detection.center[0]), int(detection.center[1])
            r = int(round(detection.radius))

            # Faint line from image center to sun center (yellow).
            cv2.line(canvas, (icx, icy), (scx, scy), YELLOW, 1, cv2.LINE_AA)

            # Circle of the detected radius (green).
            if r > 0:
                cv2.circle(canvas, (scx, scy), r, GREEN, 2, cv2.LINE_AA)

            # Filled dot at the sun center (red).
            cv2.circle(canvas, (scx, scy), 4, RED, -1, cv2.LINE_AA)

        # Status text (top-left).
        dx = detection.dx if detection is not None else 0.0
        dy = detection.dy if detection is not None else 0.0
        radius = detection.radius if detection is not None else 0.0
        status = detection.status if detection is not None else "No detection"

        lines = [
            "dx: {:.1f}".format(dx),
            "dy: {:.1f}".format(dy),
            "radius: {:.1f}".format(radius),
            "status: {}".format(status),
        ]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.5
        thickness = 1
        y = 20
        for line in lines:
            cv2.putText(
                canvas, line, (10, y), font, scale, WHITE, thickness, cv2.LINE_AA
            )
            y += 18
    except Exception:
        # On any drawing failure, return whatever canvas we have.
        return canvas

    return canvas
