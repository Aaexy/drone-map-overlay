"""Minimal MP4/MOV sample-table reader.

Used to pull the small telemetry track out of a large video without reading the whole
file: the `moov` box says exactly where each telemetry sample lives, so only those few
megabytes are read (seconds instead of minutes on an SD card).
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, Optional


@dataclass
class TrackIndex:
    handler: bytes = b""  # e.g. b"vide", b"soun", b"meta"
    name: str = ""  # handler name, e.g. "CAM meta"
    format: bytes = b""  # sample entry format, e.g. b"djmd", b"hvc1"
    timescale: int = 0
    samples: list[tuple[int, int, int]] = field(default_factory=list)  # (offset, size, dts)


def _boxes(buf: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[tuple[bytes, int, int]]:
    """(type, payload_start, box_end) for each box in buf[start:end]."""
    end = len(buf) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def _child(buf: bytes, start: int, end: int, typ: bytes) -> Optional[tuple[int, int]]:
    for t, s, e in _boxes(buf, start, end):
        if t == typ:
            return s, e
    return None


def _read_moov(f: BinaryIO) -> Optional[bytes]:
    """Find the top-level moov box by hopping over the others (no full read)."""
    f.seek(0, 2)
    file_end = f.tell()
    pos = 0
    while pos + 8 <= file_end:
        f.seek(pos)
        hdr = f.read(16)
        if len(hdr) < 8:
            return None
        size, typ = struct.unpack_from(">I4s", hdr, 0)
        hlen = 8
        if size == 1:
            size = struct.unpack_from(">Q", hdr, 8)[0]
            hlen = 16
        elif size == 0:
            size = file_end - pos
        if size < hlen:
            return None
        if typ == b"moov":
            if size > 512 * 1024 * 1024:  # not a sane moov
                return None
            f.seek(pos + hlen)
            return f.read(size - hlen)
        pos += size
    return None


def _parse_trak(buf: bytes, s: int, e: int) -> Optional[TrackIndex]:
    tr = TrackIndex()
    mdia = _child(buf, s, e, b"mdia")
    if not mdia:
        return None
    mdhd = _child(buf, *mdia, b"mdhd")
    if mdhd:
        version = buf[mdhd[0]]
        off = mdhd[0] + (20 if version == 1 else 12)
        tr.timescale = struct.unpack_from(">I", buf, off)[0]
    hdlr = _child(buf, *mdia, b"hdlr")
    if hdlr:
        tr.handler = buf[hdlr[0] + 8 : hdlr[0] + 12]
        tr.name = buf[hdlr[0] + 24 : hdlr[1]].strip(b"\x00").decode("latin-1", "replace").strip()
    minf = _child(buf, *mdia, b"minf")
    stbl = _child(buf, *minf, b"stbl") if minf else None
    if not stbl:
        return tr
    stsd = _child(buf, *stbl, b"stsd")
    if stsd and stsd[1] - stsd[0] >= 16:
        tr.format = buf[stsd[0] + 12 : stsd[0] + 16]

    stsz = _child(buf, *stbl, b"stsz")
    stsc = _child(buf, *stbl, b"stsc")
    stco = _child(buf, *stbl, b"stco")
    co64 = _child(buf, *stbl, b"co64")
    stts = _child(buf, *stbl, b"stts")
    if not (stsz and stsc and (stco or co64) and stts):
        return tr

    fixed, count = struct.unpack_from(">II", buf, stsz[0] + 4)
    sizes = [fixed] * count if fixed else list(struct.unpack_from(">%dI" % count, buf, stsz[0] + 12))
    if co64:
        n = struct.unpack_from(">I", buf, co64[0] + 4)[0]
        chunks = list(struct.unpack_from(">%dQ" % n, buf, co64[0] + 8))
    else:
        n = struct.unpack_from(">I", buf, stco[0] + 4)[0]
        chunks = list(struct.unpack_from(">%dI" % n, buf, stco[0] + 8))
    n_sc = struct.unpack_from(">I", buf, stsc[0] + 4)[0]
    runs = [struct.unpack_from(">III", buf, stsc[0] + 8 + 12 * i) for i in range(n_sc)]
    n_tt = struct.unpack_from(">I", buf, stts[0] + 4)[0]
    deltas = []
    for i in range(n_tt):
        c, d = struct.unpack_from(">II", buf, stts[0] + 8 + 8 * i)
        deltas.extend([d] * c)

    samples = []
    i = 0
    dts = 0
    for r, (first, per_chunk, _desc) in enumerate(runs):
        last = runs[r + 1][0] - 1 if r + 1 < len(runs) else len(chunks)
        for chunk in range(first, last + 1):
            off = chunks[chunk - 1]
            for _ in range(per_chunk):
                if i >= len(sizes):
                    break
                samples.append((off, sizes[i], dts))
                off += sizes[i]
                dts += deltas[i] if i < len(deltas) else 0
                i += 1
    tr.samples = samples
    return tr


def tracks(path: str) -> list[TrackIndex]:
    """All tracks of an MP4/MOV with their sample tables. Empty if it can't be read."""
    try:
        with open(path, "rb") as f:
            moov = _read_moov(f)
    except OSError:
        return []
    if not moov:
        return []
    out = []
    for typ, s, e in _boxes(moov):
        if typ == b"trak":
            try:
                t = _parse_trak(moov, s, e)
            except (struct.error, IndexError):
                t = None
            if t is not None:
                out.append(t)
    return out


def read_samples(path: str, track: TrackIndex) -> Iterator[tuple[float, bytes]]:
    """(time in seconds, payload) for each sample of `track`.

    Each sample is read on its own, unbuffered: telemetry samples are a few hundred bytes
    spread every ~200 KB through the file, so a read-ahead buffer would end up pulling in
    most of the video from the card.
    """
    scale = track.timescale or 1
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        for off, size, dts in track.samples:
            data = _pread(fd, size, off)
            if len(data) == size:
                yield dts / scale, data
    finally:
        os.close(fd)


def _pread(fd: int, size: int, off: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, size, off)
    os.lseek(fd, off, os.SEEK_SET)  # Windows has no pread
    return os.read(fd, size)
