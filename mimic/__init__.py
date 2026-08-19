"""Lokaler Mimic-TTS-Dienst."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mimic-tts")
except PackageNotFoundError:  # direkt aus einem unverpackten Quellbaum
    __version__ = "0+uninstalled"
