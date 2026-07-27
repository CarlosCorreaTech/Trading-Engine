"""Signal detection: turns warehouse metrics into classified, evidenced signals."""

from src.detection.signal import Classification, Signal, SignalType
from src.detection.runner import detect_all

__all__ = ["Classification", "Signal", "SignalType", "detect_all"]
