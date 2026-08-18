"""Binäre Mimic-Rahmen und HTTP-Hilfen ohne Modellabhängigkeiten."""

from __future__ import annotations

import http.client
import json
import struct
from typing import BinaryIO, Iterator

MEDIA_TYPE = "application/vnd.mimic.frames"
FRAME_HEADER = struct.Struct(">cI")
MAX_FRAME_BYTES = 64 * 1024 * 1024
MODES = ("mf", "soar", "qwen")


class ProtocolError(Exception):
    pass


def encode_frame(kind: str | bytes, payload: bytes) -> bytes:
    kind_b = kind.encode("ascii") if isinstance(kind, str) else kind
    if kind_b not in (b"H", b"A", b"E") or len(kind_b) != 1:
        raise ValueError("unbekannter Rahmentyp")
    return FRAME_HEADER.pack(kind_b, len(payload)) + payload


def json_frame(kind: str, value: dict) -> bytes:
    return encode_frame(kind, json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def read_exact(stream: BinaryIO, size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part:
            raise EOFError("unerwartetes Ende")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def read_frame(stream: BinaryIO) -> tuple[str, bytes]:
    kind, size = FRAME_HEADER.unpack(read_exact(stream, FRAME_HEADER.size))
    if size > MAX_FRAME_BYTES:
        raise ProtocolError("Rahmen zu gross")
    if kind not in (b"H", b"A", b"E"):
        raise ProtocolError("unbekannter Rahmentyp")
    return kind.decode("ascii"), read_exact(stream, size)


def iter_http_chunks(response: http.client.HTTPResponse) -> Iterator[bytes]:
    """Liest dekodierte HTTP-Chunks; http.client entfernt die Chunk-Grenzen."""
    while True:
        block = response.read(64 * 1024)
        if not block:
            return
        yield block


def write_chunk(stream: BinaryIO, data: bytes) -> None:
    stream.write(f"{len(data):X}\r\n".encode("ascii"))
    stream.write(data)
    stream.write(b"\r\n")
    stream.flush()


def finish_chunks(stream: BinaryIO) -> None:
    stream.write(b"0\r\n\r\n")
    stream.flush()
