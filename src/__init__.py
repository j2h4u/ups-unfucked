"""UPS Battery Monitor package.

Core infrastructure for reliable UPS telemetry collection and battery modeling.
"""

from .nut_client import NUTClient

__all__ = ['NUTClient']
