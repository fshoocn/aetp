"""Agent resource extension plugins shipped with the platform."""

from .power import PowerResourceProvider
from .serial import SerialResourceProvider
from .vector_can import VectorCanResourceProvider, scan_vector_vehicle

__all__ = [
    "PowerResourceProvider",
    "SerialResourceProvider",
    "VectorCanResourceProvider",
    "scan_vector_vehicle",
]
