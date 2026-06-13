"""mount_control.py — Serial mount control for the Simple Solar Guider.

Provides a clean abstract :class:`MountInterface` so alternative backends
(ASCOM, Alpaca, native ZWO protocol, ...) can be added later by subclassing,
plus a concrete :class:`SerialMount` that speaks LX200-style ASCII commands
over a serial COM port.

IMPORTANT HARDWARE NOTE
-----------------------
The ZWO AM3 mount may NOT accept raw LX200 commands over a plain serial link;
ZWO uses its own protocol and is typically driven via ASCOM/Alpaca on Windows.
:class:`SerialMount` is therefore the documented *seam* in this project: it is
where the real ZWO protocol, or an ASCOM/Alpaca client, should be plugged in by
adding a sibling subclass of :class:`MountInterface`. The LX200 implementation
here is a working prototype suitable for testing the rest of the app and any
mount that does understand the LX200 guide/slew command set.

Direction strings used everywhere are the single chars: "N", "S", "E", "W".
"""

from __future__ import annotations

import logging

import serial
from serial.tools import list_ports

__all__ = ["MountInterface", "SerialMount", "list_serial_ports"]

# Valid direction strings (uppercase as used by the public API).
_VALID_DIRECTIONS = ("N", "S", "E", "W")

# Hard safety bounds for guide pulses, in milliseconds.
_PULSE_MIN_MS = 0
_PULSE_MAX_MS = 1000


class MountInterface:
    """Abstract base class for mount backends.

    Subclass this to add new transports/protocols (e.g. ASCOM, Alpaca, native
    ZWO). All methods are intended to be overridden; the base implementations
    simply raise :class:`NotImplementedError`.
    """

    def connect(self) -> bool:
        """Open the connection. Return True on success, False otherwise."""
        raise NotImplementedError

    def disconnect(self) -> None:
        """Close the connection. Must never raise."""
        raise NotImplementedError

    def is_connected(self) -> bool:
        """Return True if the mount is currently connected."""
        raise NotImplementedError

    def move(self, direction: str) -> None:
        """Start a continuous slew in ``direction`` ("N"/"S"/"E"/"W").

        Continuous motion: the caller is responsible for pairing this with
        :meth:`stop`.
        """
        raise NotImplementedError

    def stop(self) -> None:
        """Stop ALL motion immediately."""
        raise NotImplementedError

    def pulse(self, direction: str, ms: int) -> None:
        """Issue a bounded, self-terminating guide pulse in ``direction``."""
        raise NotImplementedError


def list_serial_ports() -> list[str]:
    """Return the device names of available serial ports.

    Uses :func:`serial.tools.list_ports.comports`. Never raises — on any error
    an empty list is returned.
    """
    try:
        return [port.device for port in list_ports.comports()]
    except Exception:  # pragma: no cover - defensive; enumeration rarely fails
        return []


class SerialMount(MountInterface):
    """LX200-style serial mount controller.

    Stores connection parameters on construction but does NOT open the port
    until :meth:`connect` is called. Every command is routed through the private
    :meth:`_send`, which guards against the "not connected" state and logs the
    exact bytes written (to the injected logger if present, otherwise stdout).
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        logger: logging.Logger | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.logger = logger
        self._serial: serial.Serial | None = None

    # ------------------------------------------------------------------ logging
    def _log(self, message: str, level: int = logging.INFO) -> None:
        """Log ``message`` via the injected logger if any, else print it.

        Used for every command sent (so the GUI log panel can mirror traffic)
        and for connection/warning notices.
        """
        if self.logger is not None:
            self.logger.log(level, message)
        else:
            print(message)

    # --------------------------------------------------------------- connection
    def connect(self) -> bool:
        """Open ``serial.Serial(port, baudrate, timeout=1)``.

        Returns True on success and False on failure. Never raises.
        """
        # If we believe we are already connected, treat as success.
        if self.is_connected():
            return True
        try:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=1)
            self._log(f"Mount connected on {self.port} @ {self.baudrate} baud")
            return True
        except Exception as exc:  # serial.SerialException and friends
            self._serial = None
            self._log(
                f"Failed to connect to mount on {self.port}: {exc}",
                level=logging.ERROR,
            )
            return False

    def disconnect(self) -> None:
        """Close the serial port if open. Never raises."""
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as exc:  # pragma: no cover - defensive
                self._log(
                    f"Error closing mount port {self.port}: {exc}",
                    level=logging.WARNING,
                )
            finally:
                self._serial = None
                self._log("Mount disconnected")

    def is_connected(self) -> bool:
        """Return True if the underlying serial port is really open."""
        try:
            return self._serial is not None and self._serial.is_open
        except Exception:  # pragma: no cover - defensive
            return False

    # ------------------------------------------------------------------- sending
    def _send(self, cmd: str) -> None:
        """Write a single ASCII command to the port, logging the exact bytes.

        Guards the "not connected" case: logs a warning and returns without
        attempting any I/O. Each command is its own write.
        """
        if not self.is_connected():
            self._log(
                f"Cannot send {cmd!r}: mount not connected",
                level=logging.WARNING,
            )
            return
        try:
            self._serial.write(cmd.encode("ascii"))  # type: ignore[union-attr]
            self._log(f"Sent: {cmd}")
        except Exception as exc:
            self._log(
                f"Error sending {cmd!r}: {exc}",
                level=logging.ERROR,
            )

    # -------------------------------------------------------------------- motion
    def move(self, direction: str) -> None:
        """Start a continuous slew: :Mn# / :Ms# / :Me# / :Mw#.

        Continuous motion MUST be paired with :meth:`stop` by the caller.
        Unknown directions are ignored (with a warning).
        """
        d = (direction or "").strip().upper()
        if d not in _VALID_DIRECTIONS:
            self._log(
                f"Ignoring move: invalid direction {direction!r}",
                level=logging.WARNING,
            )
            return
        self._send(f":M{d.lower()}#")

    def stop(self) -> None:
        """Stop ALL motion immediately: :Q#."""
        self._send(":Q#")

    def pulse(self, direction: str, ms: int) -> None:
        """Issue a self-terminating guide pulse: :Mg<d><ms4>#.

        ``<d>`` is the lowercase direction char (n/s/e/w) and ``<ms4>`` is the
        duration zero-padded to 4 digits, e.g. North 300ms -> ":Mgn0300#".

        SAFETY: ``ms`` is clamped to the range [0, 1000]; pulses with ms <= 0
        are ignored entirely (no command sent).
        """
        d = (direction or "").strip().upper()
        if d not in _VALID_DIRECTIONS:
            self._log(
                f"Ignoring pulse: invalid direction {direction!r}",
                level=logging.WARNING,
            )
            return

        # Coerce to int defensively, then clamp into the safe range.
        try:
            ms_int = int(ms)
        except (TypeError, ValueError):
            self._log(
                f"Ignoring pulse: invalid duration {ms!r}",
                level=logging.WARNING,
            )
            return

        ms_clamped = max(_PULSE_MIN_MS, min(_PULSE_MAX_MS, ms_int))
        if ms_clamped <= 0:
            # Zero/negative duration is a no-op for safety.
            self._log(
                f"Ignoring pulse {d}: duration {ms_int}ms <= 0",
                level=logging.DEBUG,
            )
            return

        self._send(f":Mg{d.lower()}{ms_clamped:04d}#")
