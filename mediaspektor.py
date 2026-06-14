#!/usr/bin/env python3
"""MediaSpektor - Reclaim disk space by replacing watched media with tiny dummy video files.

Supports Plex, Jellyfin, and Emby media servers. Applies poster overlays to mark
archived content and integrates with Radarr/Sonarr to prevent re-downloads.
"""

import argparse
import base64
import hmac
import json
import logging
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import yaml
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("mediaspektor")

# Shared HTTP session with a generous connection pool. The dashboard proxies
# every poster through the backend; on mobile a grid loads many at once, which
# exhausted the default 10-connection pool ("Connection pool is full, discarding
# connection") and dropped posters. One pooled session keeps connections warm
# and bounded.
HTTP = requests.Session()
_pool_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50)
HTTP.mount("http://", _pool_adapter)
HTTP.mount("https://", _pool_adapter)

# Cache of computed per-show total file sizes: {(server_type, show_id): bytes}.
# Computed once per show per process; reused on every /api/shows revalidate.
_SHOW_SIZE_CACHE: dict[tuple, int] = {}


def _parse_iso_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        clean_str = date_str.split(".")[0].rstrip("Z")
        return datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
    except Exception as exc:
        logger.debug("Failed to parse ISO date string '%s': %s", date_str, exc)
        return None


def _plex_external_ids(guids) -> dict[str, str | None]:
    ids: dict[str, str | None] = {"tmdb": None, "imdb": None, "tvdb": None}
    for g in (guids or []):
        gid = getattr(g, "id", "") or ""
        if gid.startswith("tmdb://"):
            ids["tmdb"] = gid[len("tmdb://"):].split("?")[0]
        elif gid.startswith("imdb://"):
            ids["imdb"] = gid[len("imdb://"):].split("?")[0]
        elif gid.startswith("tvdb://"):
            ids["tvdb"] = gid[len("tvdb://"):].split("?")[0]
    return ids


def _provider_external_ids(provider_ids: dict) -> dict[str, str | None]:
    p = {k.lower(): v for k, v in (provider_ids or {}).items()}
    return {
        "tmdb": str(p["tmdb"]) if p.get("tmdb") else None,
        "imdb": str(p["imdb"]) if p.get("imdb") else None,
        "tvdb": str(p["tvdb"]) if p.get("tvdb") else None,
    }


# ---------------------------------------------------------------------------
# Optional plexapi import
# ---------------------------------------------------------------------------
try:
    from plexapi.server import PlexServer

    HAS_PLEXAPI = True
except ImportError:
    HAS_PLEXAPI = False


# ---------------------------------------------------------------------------
# Minimal dummy video template builders
# ---------------------------------------------------------------------------
def _make_isom_box(box_type: bytes, payload: bytes = b"") -> bytes:
    size = 8 + len(payload)
    return struct.pack(">I", size) + box_type + payload


def _make_isom_fullbox(
    box_type: bytes, version: int, flags: int, payload: bytes = b""
) -> bytes:
    size = 12 + len(payload)
    return (
        struct.pack(">I", size)
        + box_type
        + bytes([version])
        + struct.pack(">I", flags)[1:]
        + payload
    )


def _build_minimal_mp4() -> bytes:
    """Build a minimal valid MP4 container (no actual decode-able frames)."""
    parts: list[bytes] = []

    # -- ftyp ---------------------------------------------------------------
    parts.append(_make_isom_box(b"ftyp", b"mp42\x00\x00\x00\x01mp42isom"))

    # -- moov / mvhd --------------------------------------------------------
    mvhd = _make_isom_fullbox(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, 1000, 3000)  # creation  # modification
        + struct.pack(">Ih", 0x00010000, 0x0100)  # timescale  # duration 3s
        + b"\x00" * 10  # rate  # volume
        + struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)  # reserved
        + b"\x00" * 24  # matrix (identity)
        + struct.pack(">I", 1)  # pre-defined
    )  # next track id

    # -- trak ---------------------------------------------------------------
    trak_payload: list[bytes] = []

    # tkhd
    tkhd = _make_isom_fullbox(
        b"tkhd",
        0,
        0x0F,
        struct.pack(">IIII", 1, 0, 0, 0)
        + struct.pack(">IIh", 0, 0, 0)
        + struct.pack(">h", 0)
        + struct.pack(">hh", 0, 0)
        + struct.pack(
            ">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
        )
        + struct.pack(">II", 320 * 0x10000, 240 * 0x10000),
    )
    trak_payload.append(tkhd)

    # mdia
    mdia_payload: list[bytes] = []

    # mdhd
    mdhd = _make_isom_fullbox(
        b"mdhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, 1000, 3000)
        + struct.pack(">h", 0x55C4)  # language "und"
        + struct.pack(">h", 0),
    )
    mdia_payload.append(mdhd)

    # hdlr
    hdlr_payload = (
        b"\x00\x00\x00\x00"
        + b"vide"
        + b"\x00" * 12
        + b"VideoHandler\x00"
    )
    hdlr = _make_isom_fullbox(b"hdlr", 0, 0, hdlr_payload)
    mdia_payload.append(hdlr)

    # minf
    minf_payload: list[bytes] = []

    # vmhd
    vmhd = _make_isom_fullbox(
        b"vmhd", 0, 1, struct.pack(">HH", 0, 0) + struct.pack(">HHH", 0, 0, 0)
    )
    minf_payload.append(vmhd)

    # dinf
    dref_entry = struct.pack(">I", 12) + b"url " + b"\x00\x00\x00\x01"
    dref_box = _make_isom_fullbox(b"dref", 0, 0, struct.pack(">I", 1) + dref_entry)
    dinf = _make_isom_box(b"dinf", dref_box)
    minf_payload.append(dinf)

    # stbl
    stbl_payload: list[bytes] = []

    # stsd — mp4v sample entry (86 bytes entry)
    mp4v_entry = (
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"  # reserved
        + struct.pack(">HH", 320, 240)  # width, height
        + b"\x00\x48\x00\x00\x00\x48\x00\x00"  # h/v resolution
        + b"\x00\x00\x00\x00\x00\x01"  # frame count + compressor name len
        + b"\x00" * 31  # compressor name
        + b"\x00\x18\x00\xff\xff"  # depth + predef
    )
    stsd = _make_isom_fullbox(
        b"stsd", 0, 0, struct.pack(">I", 1) + b"mp4v" + mp4v_entry
    )
    stbl_payload.append(stsd)

    # stts — 1 sample, duration 3000
    stts = _make_isom_fullbox(
        b"stts", 0, 0, struct.pack(">I", 1) + struct.pack(">II", 1, 3000)
    )
    stbl_payload.append(stts)

    # stsc — 1 chunk, 1 sample/chunk, desc index 1
    stsc = _make_isom_fullbox(
        b"stsc", 0, 0, struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1)
    )
    stbl_payload.append(stsc)

    # stsz — sample size 10
    stsz = _make_isom_fullbox(
        b"stsz", 0, 0, struct.pack(">I", 0) + struct.pack(">I", 1) + struct.pack(">I", 10)
    )
    stbl_payload.append(stsz)

    # stco — chunk offset = byte position of mdat
    # (we'll fix up later via a "co64" trick — for simplicity, point into mdat)
    stco = _make_isom_fullbox(
        b"stco", 0, 0, struct.pack(">I", 1) + struct.pack(">I", 0)  # placeholder
    )
    stbl_payload.append(stco)

    stbl = _make_isom_box(b"stbl", b"".join(stbl_payload))
    minf_payload.append(stbl)

    minf = _make_isom_box(b"minf", b"".join(minf_payload))
    mdia_payload.append(minf)
    mdia = _make_isom_box(b"mdia", b"".join(mdia_payload))
    trak_payload.append(mdia)
    trak = _make_isom_box(b"trak", b"".join(trak_payload))

    moov_body = mvhd + trak
    moov = _make_isom_box(b"moov", moov_body)

    # mdat — 10 zero bytes as fake "media data"
    mdat = struct.pack(">I", 10 + 8) + b"mdat" + b"\x00" * 10

    # fix stco chunk offset to point at mdat payload (skip 8-byte box header)
    pre_mdat = b"".join(parts) + moov
    stco_pos = pre_mdat.rindex(b"stco")
    prefix = pre_mdat[: stco_pos + 16]
    postfix = pre_mdat[stco_pos + 16 + 4 :]
    pre_mdat_fixed = prefix + struct.pack(">I", len(pre_mdat) + 8) + postfix

    return pre_mdat_fixed + mdat


def _build_minimal_mkv() -> bytes:
    """Build a minimal valid Matroska container (EBML + Segment)."""

    def _ebml_id(value: int) -> bytes:
        """Encode a variable-length EBML element ID."""
        if value < 0x80:
            return bytes([value])
        # simple fixed-length encoding for known IDs
        enc = []
        while value:
            enc.insert(0, value & 0xFF)
            value >>= 8
        # set the leading bit marker
        length = len(enc)
        if length == 1:
            enc[0] |= 0x80
        elif length == 2:
            enc[0] = 0x40 | (enc[0] & 0x3F)
        elif length == 3:
            enc[0] = 0x20 | (enc[0] & 0x1F)
        elif length == 4:
            enc[0] = 0x10 | (enc[0] & 0x0F)
        return bytes(enc)

    def _ebml_size(value: int) -> bytes:
        """Encode EBML variable-length size."""
        if value < 0x7F:
            return bytes([0x80 | value])
        # simple encoding
        needed = max(1, (value.bit_length() + 7) // 8)
        raw = value.to_bytes(needed, "big")
        marker = 0x80 >> (needed - 1) if needed <= 8 else 0x01
        return bytes([marker | raw[0]]) + raw[1:]

    def _ebml_element(eid: int, payload: bytes) -> bytes:
        return _ebml_id(eid) + _ebml_size(len(payload)) + payload

    # EBML header
    ebml_version = _ebml_element(0x4286, b"\x01")
    ebml_read_version = _ebml_element(0x42F7, b"\x01")
    ebml_max_id_length = _ebml_element(0x42F2, b"\x04")
    ebml_max_size_length = _ebml_element(0x42F3, b"\x08")
    doc_type = _ebml_element(0x4282, b"matroska")
    doc_type_version = _ebml_element(0x4287, b"\x04")
    doc_type_read_version = _ebml_element(0x4285, b"\x02")
    ebml_header = _ebml_element(
        0x1A45DFA3,
        ebml_version
        + ebml_read_version
        + ebml_max_id_length
        + ebml_max_size_length
        + doc_type
        + doc_type_version
        + doc_type_read_version,
    )

    # Segment content
    # Info
    timescale = _ebml_element(0x2AD7B1, struct.pack(">I", 1000000))
    duration = _ebml_element(0x4489, struct.pack(">f", 3.0))  # 3 seconds
    info = _ebml_element(0x1549A966, timescale + duration)

    # Tracks
    track_number = _ebml_element(0xD7, b"\x01")
    track_uid = _ebml_element(0x73C5, b"\x01")
    track_type = _ebml_element(0x83, b"\x01")  # video
    codec_id = _ebml_element(0x86, b"V_MPEG4/ISO/AVC")
    video = _ebml_element(0xE0, b"")
    track_entry = _ebml_element(
        0xAE, track_number + track_uid + track_type + codec_id + video
    )
    tracks = _ebml_element(0x1654AE6B, track_entry)

    # Cluster (empty — no actual frames)
    cluster_timecode = _ebml_element(0xE7, b"\x00\x00")
    cluster = _ebml_element(0x1F43B675, cluster_timecode)

    segment = _ebml_element(0x18538067, info + tracks + cluster)

    return ebml_header + segment


def _build_minimal_avi() -> bytes:
    """Build a minimal valid AVI container."""
    # RIFF header helpers
    def _chunk(fourcc: bytes, data: bytes) -> bytes:
        return fourcc + struct.pack("<I", len(data)) + data

    def _list(list_type: bytes, data: bytes) -> bytes:
        return b"LIST" + struct.pack("<I", len(data) + 4) + list_type + data

    # Main avih
    avih = b"avih" + struct.pack(
        "<IIIIIIIIIIIIII",
        1000000 // 30,  # dwMicroSecPerFrame (~30 fps)
        0,  # dwMaxBytesPerSec
        0,  # dwPaddingGranularity
        0x10,  # dwFlags (has index)
        1,  # dwTotalFrames
        0,  # dwInitialFrames
        1,  # dwStreams
        0,  # dwSuggestedBufferSize
        320,  # dwWidth
        240,  # dwHeight
        0,  # dwReserved[0..3]
        0,
        0,
        0,
    )

    # strl for video stream
    strh = b"strh" + struct.pack(
        "<4s4sIHHIIIIIIIIhhhh",
        b"vids",  # fccType
        b"mp4v",  # fccHandler
        0,  # dwFlags
        0,  # wPriority
        0,  # wLanguage
        0,  # dwInitialFrames
        1000,  # dwScale
        30000,  # dwRate (30 fps)
        0,  # dwStart
        1,  # dwLength
        0,  # dwSuggestedBufferSize
        0,  # dwQuality
        0,  # dwSampleSize
        0,  # rcFrame.left
        0,  # rcFrame.top
        320,  # rcFrame.right
        240,  # rcFrame.bottom
    )

    # BITMAPINFOHEADER for strf
    strf = b"strf" + struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        320,  # biWidth
        240,  # biHeight
        1,  # biPlanes
        24,  # biBitCount
        0x00000000,  # biCompression (BI_RGB = 0)
        320 * 240 * 3,  # biSizeImage
        0,  # biXPelsPerMeter
        0,  # biYPelsPerMeter
        0,  # biClrUsed
        0,  # biClrImportant
    )

    strl = _list(b"strl", strh + strf)
    hdrl = _list(b"hdrl", avih + strl)

    # movi list — minimal dummy frame
    dummy_frame = b"\x00\x00\x00\x00"  # 4 byte dummy
    movi_entry = b"00db" + struct.pack("<I", len(dummy_frame)) + dummy_frame
    movi = _list(b"movi", movi_entry)

    # idx1 — 1 index entry
    idx1_entry = struct.pack(
        "<4sIII",
        b"00db",  # ckid
        0x10,  # flags (AVIIF_KEYFRAME)
        len(hdrl) + 12,  # offset from movi start
        len(dummy_frame),  # size
    )
    idx1 = b"idx1" + struct.pack("<I", 16) + idx1_entry

    riff_data = hdrl + movi + idx1
    riff = _chunk(b"RIFF", b"AVI " + riff_data)
    return riff


# ---------------------------------------------------------------------------
# Base64-encoded dummy video templates
# ---------------------------------------------------------------------------
DUMMY_VIDEOS: dict[str, str] = {
    ".mp4": "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAQTbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAJxAAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAz10cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAJxAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAABQAAAALQAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAACcQAABAAAABAAAAAAK1bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAABAAAACgABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAACYG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAiBzdGJsAAAAsHN0c2QAAAAAAAAAAQAAAKBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAABQAC0ABIAAAASAAAAAAAAAABFUxhdmM2Mi4yOC4xMDEgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAf/+EAGWdkAB+s2UBQBboQAAADABAAAAMAQPGDGWABAAZo6+PLIsD9+PgAAAAAFGJ0cnQAAAAAAABTPwAAAAAAAAAYc3R0cwAAAAAAAAABAAAAFAAAIAAAAAAUc3RzcwAAAAAAAAABAAAAAQAAAKhjdHRzAAAAAAAAABMAAAABAABAAAAAAAEAAKAAAAAAAQAAQAAAAAABAAAAAAAAAAEAACAAAAAAAQAAoAAAAAABAABAAAAAAAEAAAAAAAAAAQAAIAAAAAABAACgAAAAAAEAAEAAAAAAAQAAAAAAAAABAAAgAAAAAAEAAKAAAAAAAQAAQAAAAAABAAAAAAAAAAEAACAAAAAAAQAAgAAAAAACAAAgAAAAABxzdHNjAAAAAAAAAAEAAAABAAAAFAAAAAEAAABkc3RzegAAAAAAAAAAAAAAFAAAZGYAAAChAAAANAAAADQAAAAlAAAALwAAACgAAAAlAAAAJQAAAEEAAAAoAAAAJQAAACUAAAA7AAAAKAAAACUAAAAlAAAALgAAACcAAAAlAAAAFHN0Y28AAAAAAAAAAQAABEMAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjYyLjEyLjEwMQAAAAhmcmVlAABoF21kYXQAAAKuBgX//6rcRem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY1IHIzMjIyIGIzNTYwNWEgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDI1IC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgzOjB4MTEzIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRjdD0xIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MjIgbG9va2FoZWFkX3RocmVhZHM9MyBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJfcHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWlnaHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTIgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAGGwZYiEABf//u6CvgU3cYDr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",
    ".mkv": "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",
    ".avi": "UklGRm6vAABBVkkgTElTVOwRAABoZHJsYXZpaDgAAAAgoQcAqGEAAAAAAAAQCQAAFAAAAAAAAAABAAAAAAAQAIACAABoAQAAAAAAAAAAAAAAAAAAAAAAAExJU1SUEAAAc3RybHN0cmg4AAAAdmlkc0ZNUDQAAAAAAAAAAAAAAAABAAAAAgAAAAAAAAAUAAAA7EcAAP////8AAAAAAAAAAIACaAFzdHJmKAAAACgAAACAAgAAaAEAAAEAGABGTVA0AIwKAAAAAAAAAAAAAAAAAAAAAABKVU5LGBAAAAQAAAAAAAAAMDBkYwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABKVU5LBAEAAG9kbWxkbWxo+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAATElTVBoAAABJTkZPSVNGVA4AAABMYXZmNjIuMTIuMTAxAEpVTkv4AwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA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wDPspRL3//AaTqm6m9OqWB2laVeVsouclTe31i/d+s7Ht58khgIQ9YaHStSq+JKZkfp06m/xMn0S+h/J+ev/KA8HGjireUXDsYEJOpaVt6qEHQD/CGnzykce80kqTGGLc7/ZCy3dnlNU1Y5n0/Jh2B75eOwbuNxptguag8/3QMD9pW22pbaG32N9o43vdUb9RynSg7VF5eOhx7cYT0PtbmNt41szc1WmD/0jOeZkvhzmFslIm+yDPwBivKzvC5iMj0DyUQPNq289glfut4OWcU//sXy8yWrcznXlxLCBVY9Bu4wkSlqbyb7CVWnTKBxv9xv31U/3sbs/7fIJZsOhdfJCTZd0Qmx01jTF0efSezOh966xfSQRRxLTXeECyYdNsMl/7fcViQJRdnO9LUjSv37Vhta3FCFH2Ob7IYSCQPS7R1vqmpYB5gQkpX+X1aqkP0wGLG1+h58cIDq4+AMS+TjxlOH3U2J/J1eJy9WrHtVqq0IND+KGF1xxnedvP8kc3yRCCQOwYPmwOfpcIEHnx8qL81sc6soVVtMOJmcwC6hn/avkh8XsAxapYD/C73hA0QY19tvQ/7g4HG75qDi+UlqnFM3knYdb7GUbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfY22/jbb7G238ctv43Lfxtt/Jz3PZ/IzzPN8kZ5nm/kZ7Hm+x5t/G238bbfxtt/G232Ntv422+xtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G237AADuBEYJdt8jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/cAMDBkY30LAAAAAAG2XwI//////+8AAIUCf//HGbODnNGjg60AzODnQDMaHBzmjRwbZs5/////WyAd/Txz/HX0+cYyH//fAACPAn/9D9p7aeb9p//2vp4YXlP4dOf/3wAAlAJ/+h/T209//rfT////1vp///2Pp7/7AACeAn/187p38abU225J228kkbtrEhOIa4xD+tIzoxYLZdKWRYPSVfe6vvUL2YtUYDAo9IAlL3V96Mlw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2Du2VFaVnCtG0hxDNODnDtFiUpzedclB2d6FZ1nZB3WmOkhROIOaU4M/////bzQ7xqdHWhGR1rNf//sAAO4Cf/////+/ADAwZGOzAgAAAAABtmsBH//////3AACFAn//7t7g3/////////0AAI8Cf/3n/9wd//4AAJQCf/u//+7H////////7wAAngJ/9hM7+OPoOPg4UWMFiOMHEZ/9y//fAACjAn/vf7W0Dj47wcKzT27JpMPgcZYwSv/////taIwP8B1FMfkbGd//+wAArQJ//962sKB3g5saiZ6//+8AALICf//F2fFoMbFtGxebF5tqHFH/////i8+83Q3Q3X//9wAAvAJ//+Pw8BcwosXcDgIgcEwzB7mADN18LAo//+8AAMECf/////+/AADGAn////////1MJBTWwHUWVjQHEgOHAmWwdQpUwFgvo/FSmDhMCaPwFxtnQRRONsgBaLYO4sFv/78AANACf/c2sSFLeDhkhYoC9TC8PHIWKgxtYDVNgBZW8fg4XDbKQcSkKuDhqDk/WhkNcHEAsDAb4OSicHBSVgNHGiBwsGxKhYDhaOcQxOWoQFhI8v/vAADVAn/3tugXrOlMh4wFjOKAcJreAtnBxUbB2OtZYFY4xoFA/BWJTrOQg4UjvBbHW8F6OcHC0HEYOI7eLHL///wwwS/tmS7tkzJd21aSRe21BJF3vcZyQ1YwvYwuYwWzGNUPJyaiwwBwUBWxg4KVMLwlWxABcoWCvAeK1sI1MHCgHC9bMKYSh4FKmDj4fAsf/gAA3wJ/6mnBem0LBycbngcNELFwbjNjBwXNYOFreDhaiwcLgsBwYotYMAmBMqZMESuyAwF4vg4nAclxCThQGIL9nCIHG18HBWCSiwWASg4+vgtgcJkWDhiC7BJS4uB2HQcjGf/3AADkAn/t5I1tlA1axM3g4UM6MJw+ByM+1h2C1aysFk1gumcUBIzg4ZIzNvBww2cHBSDhNt4Us4RAmtYp////+3hV///vAADuAn//////vwAwMGRjJAEAAAAAAbZfAj//////7wAAhQJ//+4Wv/////////4AAI8Cf///61u//gAAlAJ/+7//7du////////9AACeAn//7HoJ////fwAAowJ//9rdev/////////3AACtAn//////vwAAsgJ//+8nQ3O5f/////3p2TkTmv//3wAAvAJ//////78AAMECf/////+/AADGAn////////1s/WwcNf/Www//vwAA0AJ//t5IxkXx+Djn2sHCb/9/AADVAn///tYJbeLP///8MMEv7Zku7ZMyXdtWkkXttQSRd73P2MBfrfQTf//vAADfAn//tYTMYOPbWCSxg4at4JXsYDP/3wAA5AJ/9rGwJ9vBK7WEbeAhnGARf/////9vOf//7wAA7gJ//////78wMGRj5QAAAAAAAbZrAR//////9wAAhQJ/////////////vwAAjwJ//////78AAJQCf/un//u3////////7wAAngJ//////78AAKMCf//x9P//////////fwAArQJ//////78AALICf//un//////////fAAC8An//////vwAAwQJ//////78AAMYCf////////WwE//rYJX/9AADQAn//////vwAA1QJ////////hhgl/bMl3bJmS7tq0ki9tqCSLve5////9AADfAn//+1g43///fwAA5AJ/////////////vwAA7gJ//////78AMDBkY94AAAAAAAG2XwI//////+8AAIUCf////////////78AAI8Cf/////+/AACUAn/7p//7t////////+8AAJ4Cf/////+/AACjAn////////////+/AACtAn//////vwAAsgJ/////////////vwAAvAJ//////78AAMECf/////+/AADGAn////////////+/AADQAn//////vwAA1QJ////////hhgl/bMl3bJmS7tq0ki9tqCSLve5////9AADfAn//+1g4V///3wAA5AJ/////////////vwAA7gJ//////78wMGRj3AAAAAAAAbZrAR//////9wAAhQJ/////////////vwAAjwJ//////78AAJQCf/un///////////fAACeAn//////vwAAowJ/////////////vwAArQJ//////78AALICf////////////78AALwCf/////+/AADBAn//////vwAAxgJ/////////////vwAA0AJ//////78AANUCf///////4YYJf2zJd2yZku7atJIvbagki73uf////QAA3wJ///tYLH//+wAA5AJ/////////////vwAA7gJ//////78wMGRj3QAAAAAAAbZfAj//////7wAAhQJ/////////////vwAAjwJ//////78AAJQCf/un///////////fAACeAn//////vwAAowJ/////////////vwAArQJ//////78AALICf////////////78AALwCf/////+/AADBAn//////vwAAxgJ/////////////vwAA0AJ//////78AANUCf///////4YYJf2zJd2yZku7atJIvbagki73uf////QAA3wJ///tYOI///78AAOQCf////////////78AAO4Cf/////+/ADAwZGPZAAAAAAABtmsBH//////3AACFAn////////////+/AACPAn//////vwAAlAJ/////////////vwAAngJ//////78AAKMCf////////////78AAK0Cf/////+/AACyAn////////////+/AAC8An//////vwAAwQJ//////78AAMYCf////////////78AANACf/////+/AADVAn///////+GGCX9syXdsmZLu2rSSL22oJIu97n////0AAN8Cf/////+/AADkAn////////////+/AADuAn//////vwAwMGRj2QAAAAAAAbZfAj//////7wAAhQJ/////////////vwAAjwJ//////78AAJQCf////////////78AAJ4Cf/////+/AACjAn////////////+/AACtAn//////vwAAsgJ/////////////vwAAvAJ//////78AAMECf/////+/AADGAn////////////+/AADQAn//////vwAA1QJ////////hhgl/bMl3bJmS7tq0ki9tqCSLve5////9AADfAn//////vwAA5AJ/////////////vwAA7gJ//////78AMDBkY9kAAAAAAAG2awEf//////cAAIUCf////////////78AAI8Cf/////+/AACUAn////////////+/AACeAn//////vwAAowJ/////////////vwAArQJ//////78AALICf////////////78AALwCf/////+/AADBAn//////vwAAxgJ/////////////vwAA0AJ//////78AANUCf///////4YYJf2zJd2yZku7atJIvbagki73uf////QAA3wJ//////78AAOQCf////////////78AAO4Cf/////+/ADAwZGPZAAAAAAABtl8CP//////vAACFAn////////////+/AACPAn//////vwAAlAJ/////////////vwAAngJ//////78AAKMCf////////////78AAK0Cf/////+/AACyAn////////////+/AAC8An//////vwAAwQJ//////78AAMYCf////////////78AANACf/////+/AADVAn///////+GGCX9syXdsmZLu2rSSL22oJIu97n////0AAN8Cf/////+/AADkAn////////////+/AADuAn//////vwAwMGRj7EcAAAAAAbABAAABtYkTAAABAAAAASAAxI2IABUUBC0UQwAAAbJMYXZjNjIuMjguMTAxAAABswARhwAAAbYWBRgl23yNtv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb98AAIUCRgl23yNtv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+K26LSbyI31L/w8SdTCflrm/PEjAWy0VN+eJGWiploqb88SMtgrymE7fniLm8pf56I3gFz2Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+K26vybyT2nvfk1Ph6Om3yNtv422/jbb+Ntv422/jfb+NtvyTT9FMyx/vp6JnM+Tz2Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/QAAjwJGCXbfI22/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+NtvyCftPe09fD0KrT/b5G238bbfxtt/G238bbfxtt/G238bbfxtt/G238N6U8/5BzKf1xN6Keno28+xtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfvAACUAkYJdt8jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+NtvyCf28FPtPXx0dtvkbbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfkHGaf1xlP9ro28+xtt/G238bbfxtt/G238bbfxtt/G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vb+3CxiYpMlwUYMPvjsEGgGsDwQghh+IyoSFY8EMdiOIw4qRlO2wlTJxK8rUzcS/8rTMquVR7fN5VMeF18PyEkoHr8GVJQQi5ouH2tggJxCVMNX8HSb7Y+1VmetlZBE9urW5flgidOLCMCEXj4fhBT/+muDsGgKIDzRZdUxOEIuHapXv++gKzdS4OLLdsUqcwhb6SGBCBsANA82CgbVfEj4GgYeD4GVCHayrzU3y78ZgliQDkn8Lw9UwDScFN3mUti7lwOAwQghphHBAHokgxZfiM0I6oRx20I4Hx2JYBujsSvpKPfgynIwPrf0tBTNKZ5TqhWo9nnt8PyIeBoChoMqLwYSE+hABuYCArA4OggtMl9St88OB1peJFSY0woa4CnrQ9VluxS1mKTgvHgOBRjkfAylgDypNB8zB74uVl6VkGUz7ULqkbbVJMBE1UOarjbQ5xssz13Jde32Mo2/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422+xtt/G232Ntv45bfxuW/jbb+TqHcsdn5IywVsspO3w+SMspOywVt+fkZY7KHN9jzb+Ntv422/jbb+Ntvsbbfxtt9jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv3AADuAkYJdt8jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/cwMGRj/AAAAAAAAbZfAj//////7wAAhQJ//7kd092/////+7X///4AAI8Cf/3n////vwAAlAJ/+9//7t////cYnhv///+/AACeAn/3T////98AAKMCf////YTd////////vwAArQJ/7p/////fAACyAn//3v//////////fwAAvAJ//////78AAMECf/////+/AADGAn/ul//+9f/////7xPX/vwAA0AJ/9Zp3//sJO/+/AADVAn/wwwS7sPt8//9y///8MMEv7Zku7ZMyXdtWkkXttQSRd73P////vwAA3wJ//////78AAOQCf////////+1jPbxn//+/AADuAn//////vzAwZGPpAAAAAAABtmsBH//////3AACFAn////////////+/AACPAn//////vwAAlAJ/+6//////71////4AAJ4Cf/df////fwAAowJ/////////////vwAArQJ/7CZP////9wAAsgJ//97//////////38AALwCf/////+/AADBAn//////vwAAxgJ/7kFf//vX//+9///ev/sAANACf/////+/AADVAn/wwwS7sPt8//////wwwS/tmS7tkzJd21aSRe21BJF3vc////+/AADfAn//////vwAA5AJ/////////////vwAA7gJ//////78AMDBkY+EAAAAAAAG2XwI//////+8AAIUCf////////////78AAI8Cf/////+/AACUAn////////////+/AACeAn//////vwAAowJ/////////////vwAArQJ/7t/////fAACyAn////////////+/AAC8An//////vwAAwQJ//////78AAMYCf////////////78AANACf/////+/AADVAn/wwwS7sPt8//////wwwS/tmS7tkzJd21aSRe21BJF3vc////+/AADfAn//////vwAA5AJ/////////////vwAA7gJ//////78AMDBkY+EAAAAAAAG2awEf//////cAAIUCf////////////78AAI8Cf/////+/AACUAn////////////+/AACeAn//////vwAAowJ/////////////vwAArQJ/7r////9/AACyAn////////////+/AAC8An//////vwAAwQJ//////78AAMYCf////////////78AANACf/////+/AADVAn/wwwS7sPt8//////wwwS/tmS7tkzJd21aSRe21BJF3vc////+/AADfAn//////vwAA5AJ/////////////vwAA7gJ//////78AMDBkY+EAAAAAAAG2XwI//////+8AAIUCf////////////78AAI8Cf/////+/AACUAn////////////+/AACeAn//////vwAAowJ/////////////vwAArQJ/7v////9/AACyAn////////////+/AAC8An//////vwAAwQJ//////78AAMYCf////////////78AANACf/////+/AADVAn/wwwS7sPt8//////wwwS/tmS7tkzJd21aSRe21BJF3vc////+/AADfAn//////vwAA5AJ/////////////vwAA7gJ//////78AMDBkY+EAAAAAAAG2awEf//////cAAIUCf////////////78AAI8Cf/////+/AACUAn////////////+/AACeAn//////vwAAowJ/////////////vwAArQJ/71////9/AACyAn////////////+/AAC8An//////vwAAwQJ//////78AAMYCf////////////78AANACf/////+/AADVAn/wwwS7sPt8//////wwwS/tmS7tkzJd21aSRe21BJF3vc////+/AADfAn//////vwAA5AJ/////////////vwAA7gJ//////78AMDBkY+AAAAAAAAG2XwI//////+8AAIUCf////////////78AAI8Cf/////+/AACUAn////////////+/AACeAn//////vwAAowJ/////////////vwAArQJ//////78AALICf////////////78AALwCf/////+/AADBAn//////vwAAxgJ/////////////vwAA0AJ//////78AANUCf/DDBLuw+3z//////DDBL+2ZLu2TMl3bVpJF7bUEkXe9z////78AAN8Cf/////+/AADkAn////////////+/AADuAn//////v2lkeDFAAQAAMDBkYxAAAAAEAAAA6TIAADAwZGMAAAAA9jIAAH0LAAAwMGRjAAAAAHw+AACzAgAAMDBkYwAAAAA4QQAAJAEAADAwZGMAAAAAZEIAAOUAAAAwMGRjAAAAAFJDAADeAAAAMDBkYwAAAAA4RAAA3AAAADAwZGMAAAAAHEUAAN0AAAAwMGRjAAAAAAJGAADZAAAAMDBkYwAAAADkRgAA2QAAADAwZGMAAAAAxkcAANkAAAAwMGRjAAAAAKhIAADZAAAAMDBkYxAAAACKSQAA7EcAADAwZGMAAAAAfpEAAPwAAAAwMGRjAAAAAIKSAADpAAAAMDBkYwAAAAB0kwAA4QAAADAwZGMAAAAAXpQAAOEAAAAwMGRjAAAAAEiVAADhAAAAMDBkYwAAAAAylgAA4QAAADAwZGMAAAAAHJcAAOAAAAA=",
}


# ---------------------------------------------------------------------------
# TMDB ID Bridge Client
# ---------------------------------------------------------------------------
class TmdbClient:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()
        self._cache: dict[tuple, dict] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, **params) -> dict:
        if self.api_key.startswith("eyJ"):
            headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
            resp = requests.get(f"{self.BASE}{path}", params=params, headers=headers, timeout=10)
        else:
            resp = requests.get(f"{self.BASE}{path}", params={"api_key": self.api_key, **params}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def find_tmdb_id(self, external_source: str, external_id: str, media_type: str) -> str | None:
        data = self._get(f"/find/{external_id}", external_source=external_source)
        key = "movie_results" if media_type == "movie" else "tv_results"
        results = data.get(key) or []
        return str(results[0]["id"]) if results else None

    def external_ids(self, media_type: str, tmdb_id: str) -> dict[str, str | None]:
        path = f"/movie/{tmdb_id}/external_ids" if media_type == "movie" else f"/tv/{tmdb_id}/external_ids"
        data = self._get(path)
        tvdb = data.get("tvdb_id")
        return {"imdb": data.get("imdb_id") or None, "tvdb": str(tvdb) if tvdb else None}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class Database:
    """SQLite state database for tracking archived items."""

    def __init__(self, db_path: str = "mediaspektor.db") -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS archived_items (
                    server_type    TEXT NOT NULL,
                    server_item_id TEXT NOT NULL,
                    title          TEXT NOT NULL,
                    media_type     TEXT NOT NULL,
                    original_path  TEXT NOT NULL,
                    original_size_bytes INTEGER NOT NULL,
                    dummy_size_bytes     INTEGER NOT NULL,
                    archived_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    backup_poster_path   TEXT,
                    backup_media_path    TEXT,
                    status         TEXT DEFAULT 'archived',
                    PRIMARY KEY (server_type, server_item_id)
                )"""
            )
            conn.commit()
        finally:
            conn.close()

    def insert(self, **kwargs: Any) -> None:
        columns = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO archived_items ({columns}) VALUES ({placeholders})",
                tuple(kwargs.values()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_item(
        self, server_type: str, item_id: str
    ) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM archived_items WHERE server_type=? AND server_item_id=?",
                (server_type, item_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_status(self, server_type: str, item_id: str, status: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE archived_items SET status=? WHERE server_type=? AND server_item_id=?",
                (status, server_type, item_id),
            )
            conn.commit()
        finally:
            conn.close()

    def item_exists(self, server_type: str, item_id: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM archived_items WHERE server_type=? AND server_item_id=?",
                (server_type, item_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        conn = sqlite3.connect(self.db_path)
        try:
            # Count each physical file once. The same movie archived across Plex +
            # Jellyfin + Emby has one row per server (same original_path); summing the
            # raw rows triple-counts the reclaimed space. Collapse to one row per path
            # first (taking the largest recorded size, matching the regenerate logic).
            row = conn.execute(
                """SELECT COUNT(*) as total_items,
                          COALESCE(SUM(orig), 0) as total_original,
                          COALESCE(SUM(dummy), 0) as total_dummy,
                          COALESCE(SUM(orig - dummy), 0) as total_saved
                   FROM (
                       SELECT original_path,
                              MAX(original_size_bytes) as orig,
                              MAX(dummy_size_bytes)    as dummy
                       FROM archived_items
                       WHERE status='archived'
                       GROUP BY original_path
                   )"""
            ).fetchone()
            return {
                "total_items": row[0],
                "total_saved_bytes": row[3],
                "total_saved_gb": row[3] / (1024**3),
                "total_original_bytes": row[1],
                "total_dummy_bytes": row[2],
            }
        finally:
            conn.close()

    def get_items_by_path(self, original_path: str, status: str | None = "archived") -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM archived_items WHERE original_path=? AND status=?",
                    (original_path, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM archived_items WHERE original_path=?",
                    (original_path,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Abstract Media Server Connector
# ---------------------------------------------------------------------------
class BaseMediaServer(ABC):
    def __init__(self, config: dict) -> None:
        self.config = config
        self.server_type: str = ""

    def _match_view(self, name_to_id: dict[str, str], lib_name: str) -> str | None:
        """Resolve a configured library name to a Jellyfin/Emby view id
        (case-insensitive). Logs the available names on a miss so a
        library-name mismatch is never silent."""
        if lib_name in name_to_id:
            return name_to_id[lib_name]
        lower = {k.lower(): v for k, v in name_to_id.items()}
        vid = lower.get(lib_name.lower())
        if not vid:
            logger.warning(
                "%s: library '%s' not found. Available: %s",
                self.server_type, lib_name, ", ".join(name_to_id.keys()) or "(none)",
            )
        return vid

    @abstractmethod
    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def download_poster(self, item_id: str, target_path: str) -> bool: ...

    @abstractmethod
    def upload_poster(self, item_id: str, source_path: str) -> bool: ...

    @abstractmethod
    def trigger_library_scan(self, media_type: str | None = None, item_id: str | None = None) -> None: ...

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        return []

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        return []

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        return []

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        return []

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        return {}

    @abstractmethod
    def find_item(self, file_path: str, external_ids: dict, media_type: str) -> dict | None:
        """Locate this server's local item matching the given physical media.
        Returns this server's metadata dict, or None if no confident match."""


# ---------------------------------------------------------------------------
# Plex Connector
# ---------------------------------------------------------------------------
class PlexConnector(BaseMediaServer):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.server_type = "plex"
        if not HAS_PLEXAPI:
            raise RuntimeError(
                "plexapi library is required for Plex. Install with: pip install plexapi"
            )
        self._server = PlexServer(config["url"], config["token"])

    def _resolve_id(self, item_id: str | int) -> int | str:
        if isinstance(item_id, str) and item_id.isdigit():
            return int(item_id)
        return item_id

    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for lib_name in library_names:
            try:
                section = self._server.library.section(lib_name)
            except Exception as exc:
                logger.warning("Plex: could not find library '%s': %s", lib_name, exc)
                continue

            if section.type == "movie":
                for movie in section.search():
                    if movie.isWatched:
                        media = movie.media[0] if movie.media else None
                        parts_list = media.parts if media else []
                        file_path = parts_list[0].file if parts_list else ""
                        size = parts_list[0].size if parts_list else 0
                        if file_path:
                            labels = [l.tag.lower() for l in movie.labels] if hasattr(movie, "labels") and movie.labels else []
                            genres = [g.tag.lower() for g in movie.genres] if hasattr(movie, "genres") and movie.genres else []
                            results.append(
                                {
                                    "id": movie.ratingKey,
                                    "title": movie.title,
                                    "type": "movie",
                                    "file_path": file_path,
                                    "original_size": size,
                                    "last_watched": movie.lastViewedAt if hasattr(movie, "lastViewedAt") else None,
                                    "genres": genres,
                                    "labels": labels,
                                }
                            )
            elif section.type == "show":
                for episode in section.search(libtype="episode"):
                    if episode.isWatched:
                        media = episode.media[0] if episode.media else None
                        parts_list = media.parts if media else []
                        file_path = parts_list[0].file if parts_list else ""
                        size = parts_list[0].size if parts_list else 0
                        if file_path:
                            labels = []
                            genres = []
                            try:
                                show = episode.show()
                                if show:
                                    if hasattr(show, "labels") and show.labels:
                                        labels = [l.tag.lower() for l in show.labels]
                                    if hasattr(show, "genres") and show.genres:
                                        genres = [g.tag.lower() for g in show.genres]
                            except Exception:
                                pass
                            results.append(
                                {
                                    "id": episode.ratingKey,
                                    "title": (episode.grandparentTitle or "Unknown Show")
                                    + " - "
                                    + (episode.title or "Unknown Episode"),
                                    "type": "episode",
                                    "file_path": file_path,
                                    "original_size": size,
                                    "last_watched": episode.lastViewedAt if hasattr(episode, "lastViewedAt") else None,
                                    "genres": genres,
                                    "labels": labels,
                                }
                            )
        return results

    def download_poster(self, item_id: str, target_path: str) -> bool:
        try:
            item = self._server.fetchItem(self._resolve_id(item_id))
            if item.posterUrl:
                url = item.posterUrl
                # Plex posterUrl may be relative; build full URL
                if url.startswith("/"):
                    url = self.config["url"].rstrip("/") + url + "?X-Plex-Token=" + self.config["token"]
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                with open(target_path, "wb") as f:
                    f.write(resp.content)
                return True
            logger.warning("Plex item %s has no poster", item_id)
            return False
        except Exception as exc:
            logger.error("Plex: download poster failed for %s: %s", item_id, exc)
            return False

    def upload_poster(self, item_id: str, source_path: str) -> bool:
        try:
            item = self._server.fetchItem(self._resolve_id(item_id))
            item.uploadPoster(filepath=source_path)
            return True
        except Exception as exc:
            logger.error("Plex: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self, media_type: str | None = None, item_id: str | None = None) -> None:
        # Scope the scan to the changed item's library type so a movie change
        # doesn't kick off a TV-library scan (and vice versa). media_type None
        # falls back to scanning every configured library (used by batch runs).
        want = {"movie": "movie", "episode": "show", "show": "show"}.get(media_type)
        try:
            scanned = []
            for lib_name in self.config.get("libraries", []):
                section = self._server.library.section(lib_name)
                if want and section.type != want:
                    continue
                section.update()
                scanned.append(lib_name)
            logger.info("Plex: library scan triggered for %s", ", ".join(scanned) or "all libraries")
        except Exception as exc:
            logger.error("Plex: library scan failed: %s", exc)

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        for lib_name in library_names:
            try:
                section = self._server.library.section(lib_name)
                if section.type == "movie":
                    for movie in section.search():
                        media = movie.media[0] if movie.media else None
                        parts = media.parts if media else []
                        file_path = parts[0].file if parts else ""
                        size = parts[0].size if parts else 0
                        labels = [l.tag.lower() for l in movie.labels] if hasattr(movie, "labels") and movie.labels else []
                        genres = [g.tag.lower() for g in movie.genres] if hasattr(movie, "genres") and movie.genres else []
                        results.append({
                            "id": movie.ratingKey,
                            "title": movie.title,
                            "year": movie.year,
                            "file_path": file_path,
                            "original_size": size,
                            "last_watched": movie.lastViewedAt if hasattr(movie, "lastViewedAt") else None,
                            "is_watched": movie.isWatched,
                            "genres": genres,
                            "labels": labels,
                            "poster_path": movie.thumb
                        })
            except Exception as exc:
                logger.error("Plex get_movies error: %s", exc)
        return results

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        for lib_name in library_names:
            try:
                section = self._server.library.section(lib_name)
                if section.type == "show":
                    for show in section.search():
                        labels = [l.tag.lower() for l in show.labels] if hasattr(show, "labels") and show.labels else []
                        genres = [g.tag.lower() for g in show.genres] if hasattr(show, "genres") and show.genres else []
                        results.append({
                            "id": show.ratingKey,
                            "title": show.title,
                            "year": show.year,
                            "is_watched": show.isWatched,
                            "genres": genres,
                            "labels": labels,
                            "poster_path": show.thumb
                        })
            except Exception as exc:
                logger.error("Plex get_shows error: %s", exc)
        return results

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            show = self._server.fetchItem(self._resolve_id(show_id))
            for season in show.seasons():
                results.append({
                    "id": season.ratingKey,
                    "season_number": season.index,
                    "title": season.title,
                    "is_watched": season.isWatched,
                    "poster_path": season.thumb
                })
        except Exception as exc:
            logger.error("Plex get_seasons error: %s", exc)
        return results

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            season = self._server.fetchItem(self._resolve_id(season_id))
            for episode in season.episodes():
                media = episode.media[0] if episode.media else None
                parts = media.parts if media else []
                file_path = parts[0].file if parts else ""
                size = parts[0].size if parts else 0
                results.append({
                    "id": episode.ratingKey,
                    "episode_number": episode.index,
                    "title": episode.title,
                    "file_path": file_path,
                    "original_size": size,
                    "is_watched": episode.isWatched,
                    "last_watched": episode.lastViewedAt if hasattr(episode, "lastViewedAt") else None,
                    "poster_path": episode.thumb
                })
        except Exception as exc:
            logger.error("Plex get_episodes error: %s", exc)
        return results

    def get_show_total_size(self, show_id: str) -> int:
        try:
            show = self._server.fetchItem(self._resolve_id(show_id))
            total = 0
            for ep in show.episodes():
                for media in ep.media:
                    for part in media.parts:
                        total += part.size
            return total
        except Exception as exc:
            logger.error("Plex get_show_total_size error: %s", exc)
            return 0

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        item = self._server.fetchItem(self._resolve_id(item_id))
        if item.type == "movie":
            media = item.media[0] if item.media else None
            parts = media.parts if media else []
            file_path = parts[0].file if parts else ""
            size = parts[0].size if parts else 0
            labels = [l.tag.lower() for l in item.labels] if hasattr(item, "labels") and item.labels else []
            genres = [g.tag.lower() for g in item.genres] if hasattr(item, "genres") and item.genres else []
            return {
                "id": item.ratingKey,
                "title": item.title,
                "type": "movie",
                "file_path": file_path,
                "original_size": size,
                "last_watched": item.lastViewedAt if hasattr(item, "lastViewedAt") else None,
                "genres": genres,
                "labels": labels,
                "external_ids": _plex_external_ids(getattr(item, "guids", None)),
            }
        elif item.type == "episode":
            media = item.media[0] if item.media else None
            parts = media.parts if media else []
            file_path = parts[0].file if parts else ""
            size = parts[0].size if parts else 0
            labels = []
            genres = []
            try:
                show = item.show()
                if show:
                    if hasattr(show, "labels") and show.labels:
                        labels = [l.tag.lower() for l in show.labels]
                    if hasattr(show, "genres") and show.genres:
                        genres = [g.tag.lower() for g in show.genres]
            except Exception:
                pass
            return {
                "id": item.ratingKey,
                "title": (item.grandparentTitle or "Unknown Show") + " - " + (item.title or "Unknown Episode"),
                "type": "episode",
                "file_path": file_path,
                "original_size": size,
                "last_watched": item.lastViewedAt if hasattr(item, "lastViewedAt") else None,
                "genres": genres,
                "labels": labels,
                "external_ids": _plex_external_ids(getattr(item, "guids", None)),
            }
        raise ValueError(f"Unsupported Plex item type: {item.type}")

    def find_item(self, file_path: str, external_ids: dict, media_type: str) -> dict | None:
        try:
            libtype = "movie" if media_type == "movie" else "episode"
            for lib_name in self.config.get("libraries", []):
                try:
                    section = self._server.library.section(lib_name)
                except Exception:
                    continue
                if section.type != libtype:
                    continue
                for item in section.search(libtype=libtype, includeGuids=1):
                    try:
                        if item.media and item.media[0].parts:
                            if item.media[0].parts[0].file == file_path:
                                return self.get_item_metadata(str(item.ratingKey))
                    except Exception:
                        pass
                    if media_type == "movie":
                        item_ids = _plex_external_ids(getattr(item, "guids", None))
                        for system in ("tmdb", "imdb", "tvdb"):
                            if item_ids.get(system) and external_ids.get(system) and item_ids[system] == external_ids[system]:
                                return self.get_item_metadata(str(item.ratingKey))
        except Exception as exc:
            logger.warning("Plex find_item error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Jellyfin Connector
# ---------------------------------------------------------------------------
class JellyfinConnector(BaseMediaServer):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.server_type = "jellyfin"
        self.base_url = config["url"].rstrip("/")
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.user_id = None
        self.api_key = None
        self.headers: dict[str, str] = {}
        try:
            self.authenticate()
        except Exception as exc:
            logger.warning("Jellyfin: initial authentication failed: %s. Will retry on demand.", exc)

    def authenticate(self) -> None:
        url = urljoin(self.base_url + "/", "Users/AuthenticateByName")
        auth_header = 'MediaBrowser Client="MediaSpektor", Device="Server", DeviceId="MediaSpektorID", Version="1.0"'
        headers = {
            "Content-Type": "application/json",
            "X-Emby-Authorization": auth_header
        }
        payload = {
            "Username": self.username,
            "Pw": self.password
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self.api_key = data["AccessToken"]
        self.user_id = data["User"]["Id"]
        self.headers = {
            "X-MediaBrowser-Token": self.api_key,
        }
        logger.info("Jellyfin: Authenticated successfully for user '%s' (User ID: %s)", self.username, self.user_id)

    def _ensure_auth(self) -> None:
        if not self.api_key:
            self.authenticate()

    def _request(self, method: str, path: str, json_data: Any = None, data: Any = None, params: dict | None = None, req_headers: dict | None = None) -> Any:
        self._ensure_auth()
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = self.headers.copy()
        if req_headers:
            headers.update(req_headers)
        if json_data is not None:
            headers["Content-Type"] = "application/json"

        try:
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
            elif method.upper() == "POST":
                if json_data is not None:
                    resp = requests.post(url, headers=headers, json=json_data, params=params or {}, timeout=30)
                else:
                    resp = requests.post(url, headers=headers, data=data, params=params or {}, timeout=30)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if resp.status_code == 401:
                logger.info("Jellyfin: Received 401. Retrying authentication...")
                self.authenticate()
                headers = self.headers.copy()
                if req_headers:
                    headers.update(req_headers)
                if json_data is not None:
                    headers["Content-Type"] = "application/json"
                if method.upper() == "GET":
                    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
                elif method.upper() == "POST":
                    if json_data is not None:
                        resp = requests.post(url, headers=headers, json=json_data, params=params or {}, timeout=30)
                    else:
                        resp = requests.post(url, headers=headers, data=data, params=params or {}, timeout=30)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            if isinstance(exc, requests.exceptions.HTTPError) and exc.response.status_code == 401:
                logger.info("Jellyfin: Received 401 on raise_for_status. Retrying authentication...")
                self.authenticate()
                headers = self.headers.copy()
                if json_data is not None:
                    headers["Content-Type"] = "application/json"
                if method.upper() == "GET":
                    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
                elif method.upper() == "POST":
                    if json_data is not None:
                        resp = requests.post(url, headers=headers, json=json_data, params=params or {}, timeout=30)
                    else:
                        resp = requests.post(url, headers=headers, data=data, params=params or {}, timeout=30)
                resp.raise_for_status()
                return resp
            raise exc

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            self._ensure_auth()
        except Exception as exc:
            logger.error("Jellyfin: failed to authenticate: %s", exc)
            return results

        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
        except Exception as exc:
            logger.error("Jellyfin: failed to fetch views: %s", exc)
            return results

        name_to_id: dict[str, str] = {v["Name"]: v["Id"] for v in views}

        for lib_name in library_names:
            parent_id = name_to_id.get(lib_name)
            if not parent_id:
                logger.warning("Jellyfin: library '%s' not found", lib_name)
                continue

            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "Filters": "IsPlayed",
                        "IncludeItemTypes": "Movie,Episode",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = 0
                    if media_sources:
                        size = media_sources[0].get("Size", 0)
                    if file_path:
                        user_data = item.get("UserData", {})
                        last_played_str = user_data.get("LastPlayedDate")
                        last_watched = _parse_iso_date(last_played_str)
                        labels = [t.lower() for t in item.get("Tags", [])]
                        genres = [g.lower() for g in item.get("Genres", [])]
                        results.append(
                            {
                                "id": item["Id"],
                                "title": item.get("Name", "Unknown"),
                                "type": (
                                    "movie"
                                    if item.get("Type") == "Movie"
                                    else "episode"
                                ),
                                "file_path": file_path,
                                "original_size": size,
                                "last_watched": last_watched,
                                "genres": genres,
                                "labels": labels,
                            }
                        )
            except Exception as exc:
                logger.error(
                    "Jellyfin: error fetching items from '%s': %s", lib_name, exc
                )

        return results

    def download_poster(self, item_id: str, target_path: str) -> bool:
        try:
            resp = self._get(f"/Items/{item_id}/Images/Primary")
            with open(target_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as exc:
            logger.error("Jellyfin: download poster failed for %s: %s", item_id, exc)
            return False

    def upload_poster(self, item_id: str, source_path: str) -> bool:
        try:
            # Jellyfin's POST /Items/{id}/Images/Primary expects the body to be the
            # image bytes Base64-ENCODED (not raw binary); the Content-Type header
            # carries the real image MIME type. Sending raw bytes returns HTTP 500.
            with open(source_path, "rb") as f:
                payload = base64.b64encode(f.read())
            self._request(
                "POST",
                f"Items/{item_id}/Images/Primary",
                data=payload,
                params={"X-Emby-Client": "MediaSpektor"},
                req_headers={"Content-Type": "image/jpeg"},
            )
            return True
        except Exception as exc:
            logger.error("Jellyfin: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self, media_type: str | None = None, item_id: str | None = None) -> None:
        try:
            if item_id:
                # Refresh just the changed item so we don't scan unrelated libraries
                # (e.g. TV when a movie changed). ImageRefreshMode=None keeps the
                # badged poster we just uploaded from being overwritten.
                self._request(
                    "POST",
                    f"Items/{item_id}/Refresh",
                    params={
                        "Recursive": "false",
                        "MetadataRefreshMode": "Default",
                        "ImageRefreshMode": "None",
                        "ReplaceAllMetadata": "false",
                        "ReplaceAllImages": "false",
                    },
                )
                logger.info("Jellyfin: refresh triggered for item %s", item_id)
            else:
                self._request("POST", "Library/Refresh")
                logger.info("Jellyfin: full library scan triggered")
        except Exception as exc:
            logger.error("Jellyfin: library scan failed: %s", exc)

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            self._ensure_auth()
        except Exception as exc:
            logger.error("Jellyfin: failed to authenticate: %s", exc)
            return results
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Jellyfin views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = self._match_view(name_to_id, lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Movie",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = media_sources[0].get("Size", 0) if media_sources else 0
                    user_data = item.get("UserData", {})
                    last_played_str = user_data.get("LastPlayedDate")
                    last_watched = _parse_iso_date(last_played_str)
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "file_path": file_path,
                        "original_size": size,
                        "last_watched": last_watched,
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Jellyfin get_movies from '%s': %s", lib_name, exc)
        return results

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            self._ensure_auth()
        except Exception as exc:
            logger.error("Jellyfin: failed to authenticate: %s", exc)
            return results
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Jellyfin views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = self._match_view(name_to_id, lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Series",
                        "Fields": "UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    user_data = item.get("UserData", {})
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Jellyfin get_shows from '%s': %s", lib_name, exc)
        return results

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            self._ensure_auth()
        except Exception as exc:
            logger.error("Jellyfin: failed to authenticate: %s", exc)
            return results
        try:
            resp = self._get(
                f"/Shows/{show_id}/Seasons",
                params={"UserId": self.user_id, "Fields": "UserData"}
            )
            items = resp.json().get("Items", [])
            for item in items:
                user_data = item.get("UserData", {})
                results.append({
                    "id": item["Id"],
                    "season_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "is_watched": user_data.get("Played", False),
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Jellyfin get_seasons for %s: %s", show_id, exc)
        return results

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            self._ensure_auth()
        except Exception as exc:
            logger.error("Jellyfin: failed to authenticate: %s", exc)
            return results
        try:
            resp = self._get(
                f"/Shows/{show_id}/Episodes",
                params={
                    "SeasonId": season_id,
                    "UserId": self.user_id,
                    "Fields": "Path,MediaSources,UserData"
                }
            )
            items = resp.json().get("Items", [])
            for item in items:
                media_sources = item.get("MediaSources", [])
                file_path = item.get("Path", "")
                size = media_sources[0].get("Size", 0) if media_sources else 0
                user_data = item.get("UserData", {})
                last_played_str = user_data.get("LastPlayedDate")
                last_watched = _parse_iso_date(last_played_str)
                results.append({
                    "id": item["Id"],
                    "episode_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "file_path": file_path,
                    "original_size": size,
                    "is_watched": user_data.get("Played", False),
                    "last_watched": last_watched,
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Jellyfin get_episodes for season %s: %s", season_id, exc)
        return results

    def get_show_total_size(self, show_id: str) -> int:
        try:
            resp = self._get(f"/Items", params={
                "ParentId": show_id,
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "MediaSources",
                "UserId": self.user_id
            })
            total = 0
            for item in resp.json().get("Items", []):
                sources = item.get("MediaSources", [])
                if sources:
                    total += sources[0].get("Size", 0)
            return total
        except Exception as exc:
            logger.error("Jellyfin get_show_total_size error: %s", exc)
            return 0

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        self._ensure_auth()
        item = self._get(f"/Users/{self.user_id}/Items/{item_id}").json()
        media_sources = item.get("MediaSources", [])
        file_path = item.get("Path", "")
        size = media_sources[0].get("Size", 0) if media_sources else 0
        user_data = item.get("UserData", {})
        last_played_str = user_data.get("LastPlayedDate")
        last_watched = _parse_iso_date(last_played_str)
        labels = [t.lower() for t in item.get("Tags", [])]
        genres = [g.lower() for g in item.get("Genres", [])]
        
        item_type = item.get("Type", "")
        if item_type == "Movie":
            type_str = "movie"
            title_str = item.get("Name", "Unknown")
        elif item_type == "Episode":
            type_str = "episode"
            title_str = (item.get("SeriesName") or "Unknown Show") + " - " + (item.get("Name") or "Unknown Episode")
        else:
            type_str = item_type.lower()
            title_str = item.get("Name", "Unknown")

        return {
            "id": item["Id"],
            "title": title_str,
            "type": type_str,
            "file_path": file_path,
            "original_size": size,
            "last_watched": last_watched,
            "genres": genres,
            "labels": labels,
            "external_ids": _provider_external_ids(item.get("ProviderIds", {})),
        }

    def find_item(self, file_path: str, external_ids: dict, media_type: str) -> dict | None:
        try:
            self._ensure_auth()
            if not self.user_id:
                return None
            resp = self._get(
                f"/Users/{self.user_id}/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path,ProviderIds,MediaSources",
                },
            )
            items = resp.json().get("Items", [])
            for item in items:
                item_path = item.get("Path", "")
                if item_path and item_path == file_path:
                    return self.get_item_metadata(item["Id"])
                item_type = item.get("Type", "")
                if media_type == "movie" and item_type == "Movie":
                    item_ids = _provider_external_ids(item.get("ProviderIds", {}))
                    for system in ("tmdb", "imdb", "tvdb"):
                        if item_ids.get(system) and external_ids.get(system) and item_ids[system] == external_ids[system]:
                            return self.get_item_metadata(item["Id"])
        except Exception as exc:
            logger.warning("Jellyfin find_item error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Emby Connector
# ---------------------------------------------------------------------------
class EmbyConnector(BaseMediaServer):
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.server_type = "emby"
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.user_id = config["user_id"]
        self.headers: dict[str, str] = {
            "X-MediaBrowser-Token": self.api_key,
        }
        try:
            self._resolve_user_id()
        except Exception as exc:
            logger.warning("Emby: user id resolution failed: %s. Using configured value.", exc)

    def _resolve_user_id(self) -> None:
        """Emby's /Users/{id}/... endpoints need the user GUID, not a username.
        Accept either by resolving the configured value (name or id) via /Users."""
        configured = self.user_id
        resp = requests.get(
            urljoin(self.base_url + "/", "Users"), headers=self.headers, timeout=30
        )
        resp.raise_for_status()
        users = resp.json()
        match = next(
            (u for u in users if u.get("Id") == configured or u.get("Name") == configured),
            None,
        )
        if match:
            if match["Id"] != self.user_id:
                logger.info("Emby: resolved user '%s' to id %s", configured, match["Id"])
            self.user_id = match["Id"]
        else:
            logger.warning("Emby: could not resolve user '%s' via /Users", configured)

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        resp = requests.get(url, headers=self.headers, params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp

    def get_watched_items(
        self, library_names: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
        except Exception as exc:
            logger.error("Emby: failed to fetch views: %s", exc)
            return results

        name_to_id: dict[str, str] = {v["Name"]: v["Id"] for v in views}

        for lib_name in library_names:
            parent_id = self._match_view(name_to_id, lib_name)
            if not parent_id:
                continue

            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "Filters": "IsPlayed",
                        "IncludeItemTypes": "Movie,Episode",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = 0
                    if media_sources:
                        size = media_sources[0].get("Size", 0)
                    if file_path:
                        user_data = item.get("UserData", {})
                        last_played_str = user_data.get("LastPlayedDate")
                        last_watched = _parse_iso_date(last_played_str)
                        labels = [t.lower() for t in item.get("Tags", [])]
                        genres = [g.lower() for g in item.get("Genres", [])]
                        results.append(
                            {
                                "id": item["Id"],
                                "title": item.get("Name", "Unknown"),
                                "type": (
                                    "movie"
                                    if item.get("Type") == "Movie"
                                    else "episode"
                                ),
                                "file_path": file_path,
                                "original_size": size,
                                "last_watched": last_watched,
                                "genres": genres,
                                "labels": labels,
                            }
                        )
            except Exception as exc:
                logger.error(
                    "Emby: error fetching items from '%s': %s", lib_name, exc
                )

        return results

    def download_poster(self, item_id: str, target_path: str) -> bool:
        try:
            resp = self._get(f"/Items/{item_id}/Images/Primary")
            with open(target_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as exc:
            logger.error("Emby: download poster failed for %s: %s", item_id, exc)
            return False

    def upload_poster(self, item_id: str, source_path: str) -> bool:
        try:
            url = urljoin(
                self.base_url + "/", f"Items/{item_id}/Images/Primary"
            )
            # Emby (like Jellyfin) expects the image body Base64-ENCODED, with the
            # Content-Type header set to the real image MIME type. Raw bytes -> HTTP 500.
            with open(source_path, "rb") as f:
                payload = base64.b64encode(f.read())
            upload_headers = self.headers.copy()
            upload_headers["Content-Type"] = "image/jpeg"
            resp = requests.post(
                url,
                headers=upload_headers,
                data=payload,
                params={"X-Emby-Client": "MediaSpektor"},
                timeout=30,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Emby: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self, media_type: str | None = None, item_id: str | None = None) -> None:
        try:
            if item_id:
                # Refresh just the changed item, not every library. ImageRefreshMode=None
                # preserves the badged poster we just uploaded.
                url = urljoin(self.base_url + "/", f"Items/{item_id}/Refresh")
                requests.post(
                    url,
                    headers=self.headers,
                    params={
                        "Recursive": "false",
                        "MetadataRefreshMode": "Default",
                        "ImageRefreshMode": "None",
                        "ReplaceAllMetadata": "false",
                        "ReplaceAllImages": "false",
                    },
                    timeout=30,
                )
                logger.info("Emby: refresh triggered for item %s", item_id)
            else:
                url = urljoin(self.base_url + "/", "Library/Refresh")
                requests.post(url, headers=self.headers, timeout=30)
                logger.info("Emby: full library scan triggered")
        except Exception as exc:
            logger.error("Emby: library scan failed: %s", exc)

    def get_movies(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Emby views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = self._match_view(name_to_id, lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Movie",
                        "Fields": "Path,MediaSources,UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    media_sources = item.get("MediaSources", [])
                    file_path = item.get("Path", "")
                    size = media_sources[0].get("Size", 0) if media_sources else 0
                    user_data = item.get("UserData", {})
                    last_played_str = user_data.get("LastPlayedDate")
                    last_watched = _parse_iso_date(last_played_str)
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "file_path": file_path,
                        "original_size": size,
                        "last_watched": last_watched,
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Emby get_movies from '%s': %s", lib_name, exc)
        return results

    def get_shows(self, library_names: list[str]) -> list[dict[str, Any]]:
        results = []
        try:
            views_resp = self._get(f"/Users/{self.user_id}/Views")
            views = views_resp.json().get("Items", [])
            name_to_id = {v["Name"]: v["Id"] for v in views}
        except Exception as exc:
            logger.error("Emby views: %s", exc)
            return results

        for lib_name in library_names:
            parent_id = self._match_view(name_to_id, lib_name)
            if not parent_id:
                continue
            try:
                resp = self._get(
                    f"/Users/{self.user_id}/Items",
                    params={
                        "ParentId": parent_id,
                        "Recursive": "true",
                        "IncludeItemTypes": "Series",
                        "Fields": "UserData,Tags,Genres,ProductionYear",
                    },
                )
                items = resp.json().get("Items", [])
                for item in items:
                    user_data = item.get("UserData", {})
                    labels = [t.lower() for t in item.get("Tags", [])]
                    genres = [g.lower() for g in item.get("Genres", [])]
                    results.append({
                        "id": item["Id"],
                        "title": item.get("Name", "Unknown"),
                        "year": item.get("ProductionYear"),
                        "is_watched": user_data.get("Played", False),
                        "genres": genres,
                        "labels": labels,
                        "poster_path": f"/Items/{item['Id']}/Images/Primary"
                    })
            except Exception as exc:
                logger.error("Emby get_shows from '%s': %s", lib_name, exc)
        return results

    def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            resp = self._get(
                f"/Shows/{show_id}/Seasons",
                params={"UserId": self.user_id, "Fields": "UserData"}
            )
            items = resp.json().get("Items", [])
            for item in items:
                user_data = item.get("UserData", {})
                results.append({
                    "id": item["Id"],
                    "season_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "is_watched": user_data.get("Played", False),
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Emby get_seasons for %s: %s", show_id, exc)
        return results

    def get_episodes(self, show_id: str, season_id: str) -> list[dict[str, Any]]:
        results = []
        try:
            resp = self._get(
                f"/Shows/{show_id}/Episodes",
                params={
                    "SeasonId": season_id,
                    "UserId": self.user_id,
                    "Fields": "Path,MediaSources,UserData"
                }
            )
            items = resp.json().get("Items", [])
            for item in items:
                media_sources = item.get("MediaSources", [])
                file_path = item.get("Path", "")
                size = media_sources[0].get("Size", 0) if media_sources else 0
                user_data = item.get("UserData", {})
                last_played_str = user_data.get("LastPlayedDate")
                last_watched = _parse_iso_date(last_played_str)
                results.append({
                    "id": item["Id"],
                    "episode_number": item.get("IndexNumber", 0),
                    "title": item.get("Name", "Unknown"),
                    "file_path": file_path,
                    "original_size": size,
                    "is_watched": user_data.get("Played", False),
                    "last_watched": last_watched,
                    "poster_path": f"/Items/{item['Id']}/Images/Primary"
                })
        except Exception as exc:
            logger.error("Emby get_episodes for season %s: %s", season_id, exc)
        return results

    def get_show_total_size(self, show_id: str) -> int:
        try:
            resp = self._get(f"/Users/{self.user_id}/Items", params={
                "ParentId": show_id,
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "MediaSources"
            })
            total = 0
            for item in resp.json().get("Items", []):
                sources = item.get("MediaSources", [])
                if sources:
                    total += sources[0].get("Size", 0)
            return total
        except Exception as exc:
            logger.error("Emby get_show_total_size error: %s", exc)
            return 0

    def get_item_metadata(self, item_id: str) -> dict[str, Any]:
        item = self._get(f"/Users/{self.user_id}/Items/{item_id}").json()
        media_sources = item.get("MediaSources", [])
        file_path = item.get("Path", "")
        size = media_sources[0].get("Size", 0) if media_sources else 0
        user_data = item.get("UserData", {})
        last_played_str = user_data.get("LastPlayedDate")
        last_watched = _parse_iso_date(last_played_str)
        labels = [t.lower() for t in item.get("Tags", [])]
        genres = [g.lower() for g in item.get("Genres", [])]
        
        item_type = item.get("Type", "")
        if item_type == "Movie":
            type_str = "movie"
            title_str = item.get("Name", "Unknown")
        elif item_type == "Episode":
            type_str = "episode"
            title_str = (item.get("SeriesName") or "Unknown Show") + " - " + (item.get("Name") or "Unknown Episode")
        else:
            type_str = item_type.lower()
            title_str = item.get("Name", "Unknown")

        return {
            "id": item["Id"],
            "title": title_str,
            "type": type_str,
            "file_path": file_path,
            "original_size": size,
            "last_watched": last_watched,
            "genres": genres,
            "labels": labels,
            "external_ids": _provider_external_ids(item.get("ProviderIds", {})),
        }

    def find_item(self, file_path: str, external_ids: dict, media_type: str) -> dict | None:
        if not self.config.get("user_id") or not self.config.get("api_key"):
            logger.error("Emby: user_id and api_key required for cross-server matching")
            return None
        try:
            resp = self._get(
                f"/Users/{self.user_id}/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path,ProviderIds,MediaSources",
                },
            )
            items = resp.json().get("Items", [])
            for item in items:
                item_path = item.get("Path", "")
                if item_path and item_path == file_path:
                    return self.get_item_metadata(item["Id"])
                item_type = item.get("Type", "")
                if media_type == "movie" and item_type == "Movie":
                    item_ids = _provider_external_ids(item.get("ProviderIds", {}))
                    for system in ("tmdb", "imdb", "tvdb"):
                        if item_ids.get(system) and external_ids.get(system) and item_ids[system] == external_ids[system]:
                            return self.get_item_metadata(item["Id"])
        except Exception as exc:
            logger.warning("Emby find_item error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Radarr / Sonarr Integration
# ---------------------------------------------------------------------------
class RadarrClient:
    def __init__(self, config: dict) -> None:
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.headers: dict[str, str] = {"X-Api-Key": self.api_key}

    def unmonitor_movie_by_path(self, file_path: str, external_ids: dict | None = None) -> bool:
        return self.set_movie_monitored(file_path, monitored=False, external_ids=external_ids)

    def set_movie_monitored(self, file_path: str, monitored: bool, external_ids: dict | None = None) -> bool:
        """Set a movie's monitored flag in Radarr (False on archive so it won't
        re-download; True to let *Arr fetch the real file again)."""
        try:
            url = urljoin(self.base_url + "/", "api/v3/movie")
            resp = requests.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            movies = resp.json()

            match = self._match_movie(movies, file_path, external_ids or {})
            if not match:
                logger.warning(
                    "Radarr: no matching movie for path '%s' (ids=%s) among %d movies. "
                    "If Radarr mounts the library at a different path than the media server, "
                    "set a TMDB key so MediaSpektor can match by ID instead of path.",
                    file_path, external_ids or {}, len(movies),
                )
                return False

            if match.get("monitored", None) == monitored:
                logger.info("Radarr: movie id=%s already monitored=%s", match["id"], monitored)
                return True

            match["monitored"] = monitored
            put_url = urljoin(self.base_url + "/", f"api/v3/movie/{match['id']}")
            put_resp = requests.put(put_url, headers=self.headers, json=match, timeout=30)
            put_resp.raise_for_status()
            logger.info("Radarr: set monitored=%s for movie id=%s title=%s", monitored, match["id"], match.get("title"))
            return True
        except Exception as exc:
            logger.error("Radarr: error setting monitored state: %s", exc)
            return False

    def get_movie_monitored(self, file_path: str, external_ids: dict | None = None) -> bool | None:
        """Return the current monitored flag for the matched movie, or None if not found."""
        try:
            url = urljoin(self.base_url + "/", "api/v3/movie")
            resp = requests.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            match = self._match_movie(resp.json(), file_path, external_ids or {})
            return bool(match.get("monitored")) if match else None
        except Exception as exc:
            logger.error("Radarr: error reading monitored state: %s", exc)
            return None

    @staticmethod
    def _match_movie(movies: list[dict], file_path: str, external_ids: dict) -> dict | None:
        """Find the Radarr movie for an archived file. Prefer ID matches (robust to
        differing path roots between Radarr and the media servers), then fall back to
        path heuristics: full-prefix, shared folder-leaf, or matching file basename."""
        tmdb = str(external_ids.get("tmdb")) if external_ids.get("tmdb") else None
        imdb = str(external_ids.get("imdb")).lower() if external_ids.get("imdb") else None

        if tmdb:
            for m in movies:
                if str(m.get("tmdbId")) == tmdb:
                    return m
        if imdb:
            for m in movies:
                if (m.get("imdbId") or "").lower() == imdb:
                    return m

        norm_path = os.path.normpath(file_path).lower()
        file_name = os.path.basename(norm_path)
        file_dir_leaf = os.path.basename(os.path.dirname(norm_path))
        for m in movies:
            mp = os.path.normpath(m.get("path", "")).lower() if m.get("path") else ""
            if mp and (norm_path.startswith(mp) or (file_dir_leaf and os.path.basename(mp) == file_dir_leaf)):
                return m
            mf = m.get("movieFile") or {}
            rel = mf.get("relativePath") or mf.get("path") or ""
            if rel and os.path.basename(os.path.normpath(rel).lower()) == file_name:
                return m
        return None


class SonarrClient:
    def __init__(self, config: dict) -> None:
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.headers: dict[str, str] = {"X-Api-Key": self.api_key}

    def unmonitor_episode_by_path(self, file_path: str, external_ids: dict | None = None) -> bool:
        return self.set_episode_monitored(file_path, monitored=False, external_ids=external_ids)

    def _find_episodes(self, file_path: str, external_ids: dict | None = None) -> list[dict]:
        """Return the Sonarr episode record(s) whose file is this archived path.
        Matches the series by ID/path/folder-leaf and the file by path/basename."""
        url = urljoin(self.base_url + "/", "api/v3/series")
        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        series_list = resp.json()

        norm_path = os.path.normpath(file_path).lower()
        file_name = os.path.basename(norm_path)
        path_parts = set(norm_path.split(os.sep))
        ids = external_ids or {}
        tvdb = str(ids.get("tvdb")) if ids.get("tvdb") else None
        imdb = str(ids.get("imdb")).lower() if ids.get("imdb") else None
        for series in series_list:
            series_path = os.path.normpath(series.get("path", "")).lower()
            series_leaf = os.path.basename(series_path) if series_path else ""
            series_matched = (
                (tvdb and str(series.get("tvdbId")) == tvdb)
                or (imdb and (series.get("imdbId") or "").lower() == imdb)
                or (series_path and norm_path.startswith(series_path))
                or (series_leaf and series_leaf in path_parts)
            )
            if not series_matched:
                continue
            series_id = series["id"]
            file_resp = requests.get(
                urljoin(self.base_url + "/", f"api/v3/episodefile?seriesId={series_id}"),
                headers=self.headers, timeout=30,
            )
            file_resp.raise_for_status()
            episode_file_id = None
            for ep_file in file_resp.json():
                ep_file_path = os.path.normpath(ep_file.get("path", "")).lower()
                if ep_file_path == norm_path or os.path.basename(ep_file_path) == file_name:
                    episode_file_id = ep_file.get("id")
                    break
            if not episode_file_id:
                continue
            ep_resp = requests.get(
                urljoin(self.base_url + "/", f"api/v3/episode?seriesId={series_id}"),
                headers=self.headers, timeout=30,
            )
            ep_resp.raise_for_status()
            eps = [ep for ep in ep_resp.json() if ep.get("episodeFileId") == episode_file_id]
            if eps:
                return eps
        return []

    def set_episode_monitored(self, file_path: str, monitored: bool, external_ids: dict | None = None) -> bool:
        try:
            eps = self._find_episodes(file_path, external_ids)
            if not eps:
                logger.warning("Sonarr: no matching episode found for path '%s'", file_path)
                return False
            for ep in eps:
                ep["monitored"] = monitored
                put_resp = requests.put(
                    urljoin(self.base_url + "/", f"api/v3/episode/{ep['id']}"),
                    headers=self.headers, json=ep, timeout=30,
                )
                put_resp.raise_for_status()
                logger.info("Sonarr: set monitored=%s for episode id=%s", monitored, ep["id"])
            return True
        except Exception as exc:
            logger.error("Sonarr: error setting monitored state: %s", exc)
            return False

    def get_episode_monitored(self, file_path: str, external_ids: dict | None = None) -> bool | None:
        try:
            eps = self._find_episodes(file_path, external_ids)
            return bool(eps[0].get("monitored")) if eps else None
        except Exception as exc:
            logger.error("Sonarr: error reading monitored state: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Poster Overlay Engine
# ---------------------------------------------------------------------------
class PosterOverlay:
    def __init__(self, config: dict) -> None:
        aest = config.get("aesthetics", {})
        self.enabled = aest.get("enable_poster_overlay", True)
        self.banner_color = tuple(aest.get("banner_color", [8, 11, 10, 204]))
        self.border_color = tuple(
            aest.get("border_color", [62, 207, 142, 255])
        )
        self.font_name = aest.get("font_name", "DejaVuSans.ttf")
        self.font_size_ratio = aest.get("font_size_ratio", 0.045)

    def apply_overlay(
        self, image_path: str, output_path: str, gb_saved: float
    ) -> bool:
        """Apply glassmorphic banner overlay to poster image."""
        if not self.enabled:
            shutil.copy2(image_path, output_path)
            return True
        try:
            img = Image.open(image_path).convert("RGBA")
            draw = ImageDraw.Draw(img)
            width, height = img.size

            # Banner dimensions
            banner_height = int(height * 0.15)
            y_start = height - banner_height

            # Draw glassmorphic background
            overlay = Image.new("RGBA", (width, banner_height), self.banner_color)
            img.paste(overlay, (0, y_start), overlay)

            # Accent line across the top of the banner.
            draw.line([(0, y_start), (width, y_start)], fill=self.border_color, width=1)

            # Mint frame around the whole poster so the archived state reads at a glance.
            border_w = max(2, round(width / 220))
            draw.rectangle(
                [(0, 0), (width - 1, height - 1)],
                outline=self.border_color,
                width=border_w,
            )

            # Text \u2014 only show the saved figure when it's meaningful. Regenerating a
            # poster recomputes from the DB and can land at 0.0 GB, which looks broken;
            # in that case just say ARCHIVED.
            if gb_saved is not None and gb_saved >= 0.05:
                text = f"ARCHIVED \u2022 {gb_saved:.1f} GB SAVED"
            else:
                text = "ARCHIVED"
            font_size = int(height * self.font_size_ratio)
            try:
                font = ImageFont.truetype(self.font_name, font_size)
            except (OSError, IOError):
                try:
                    font = ImageFont.truetype("DejaVuSans.ttf", font_size)
                except (OSError, IOError):
                    logger.debug(
                        "Font '%s' and DejaVuSans not found, using default bitmap", self.font_name
                    )
                    font = ImageFont.load_default()

            # Center text
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            text_x = (width - text_width) // 2
            text_y = y_start + (banner_height - text_height) // 2 - bbox[1]

            draw.text(
                (text_x, text_y),
                text,
                fill=(255, 255, 255, 255),
                font=font,
            )

            img = img.convert("RGB")
            img.save(output_path, "JPEG", quality=90)
            return True
        except Exception as exc:
            logger.error("Poster overlay failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# MediaSpektor Orchestrator
# ---------------------------------------------------------------------------
class MediaSpektor:
    def __init__(self, config_path: str = "config.yaml") -> None:
        with open(config_path, "r") as f:
            self.config: dict = yaml.safe_load(f)

        # Surface weak auth posture loudly — the dashboard exposes server tokens,
        # passwords and the ability to overwrite media.
        sec = self.config.get("security", {})
        if not sec.get("enabled", False):
            logger.warning(
                "SECURITY: dashboard authentication is DISABLED — anyone who can reach "
                "the web UI can read your server tokens/passwords and archive media. "
                "Set security.enabled: true with a strong password."
            )
        elif sec.get("password", "admin") in ("", "admin"):
            logger.warning(
                "SECURITY: the dashboard is using the default 'admin' password — "
                "change security.password."
            )

        config_dir = os.path.dirname(os.path.abspath(config_path))
        db_path = os.path.join(config_dir, "mediaspektor.db")
        self.db = Database(db_path)

        self.overlay = PosterOverlay(self.config)
        # Poster (and optional media) backups. Treat an empty value or the example
        # placeholder as "unset" and default to a writable dir next to the config.
        default_backups = os.path.join(config_dir, "backups")
        backup_path = self.config.get("safety", {}).get("backup_directory") or default_backups
        if str(backup_path).startswith("/path/to"):
            backup_path = default_backups
        self.backup_dir = Path(backup_path)
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as exc:
            logger.warning(
                "Cannot create backup directory '%s': %s. Falling back to '%s'.",
                backup_path, exc, default_backups,
            )
            self.backup_dir = Path(default_backups)
            self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Initialize server connectors
        self.servers: list[BaseMediaServer] = []
        for server_cfg in self.config.get("servers", []):
            if not server_cfg.get("enabled", False):
                continue
            connector = self._create_connector(server_cfg)
            if connector:
                self.servers.append(connector)

        # Initialize *Arr clients
        self.radarr: RadarrClient | None = None
        self.sonarr: SonarrClient | None = None
        integrations = self.config.get("integrations", {})
        if integrations.get("radarr", {}).get("enabled", False):
            self.radarr = RadarrClient(integrations["radarr"])
        if integrations.get("sonarr", {}).get("enabled", False):
            self.sonarr = SonarrClient(integrations["sonarr"])

        # TMDB ID bridge (optional, key-gated)
        tmdb_key = integrations.get("tmdb", {}).get("api_key", "") or os.environ.get("TMDB_API_KEY", "")
        self.tmdb = TmdbClient(tmdb_key)

    def _create_connector(
        self, cfg: dict
    ) -> BaseMediaServer | None:
        server_type = cfg.get("type", "").lower()
        try:
            if server_type == "plex":
                return PlexConnector(cfg)
            elif server_type == "jellyfin":
                return JellyfinConnector(cfg)
            elif server_type == "emby":
                return EmbyConnector(cfg)
            else:
                logger.warning("Unknown server type: %s", server_type)
                return None
        except Exception as exc:
            logger.warning("Failed to create %s connector: %s", server_type, exc)
            return None

    def _expand_external_ids(self, media_type: str, ids: dict) -> dict:
        if media_type != "movie":
            return ids
        if not self.tmdb.enabled:
            return ids
        try:
            non_null = frozenset((k, v) for k, v in ids.items() if v)
            cache_key = (media_type, non_null)
            if cache_key in self.tmdb._cache:
                return self.tmdb._cache[cache_key]

            merged = dict(ids)
            tmdb_id = ids.get("tmdb")
            if not tmdb_id:
                if ids.get("imdb"):
                    tmdb_id = self.tmdb.find_tmdb_id("imdb_id", ids["imdb"], media_type)
                if not tmdb_id and ids.get("tvdb"):
                    tmdb_id = self.tmdb.find_tmdb_id("tvdb_id", ids["tvdb"], media_type)

            if tmdb_id:
                merged["tmdb"] = tmdb_id
                ext = self.tmdb.external_ids(media_type, tmdb_id)
                if ext.get("imdb") and not merged.get("imdb"):
                    merged["imdb"] = ext["imdb"]
                if ext.get("tvdb") and not merged.get("tvdb"):
                    merged["tvdb"] = ext["tvdb"]

            self.tmdb._cache[cache_key] = merged
            return merged
        except Exception as exc:
            logger.warning("TMDB bridge expansion failed: %s — using source IDs only", exc)
            return ids

    def _filter_items(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rules = self.config.get("rules", {})
        min_age_days = rules.get("min_age_days", 7)
        dummy_threshold_bytes = rules.get("dummy_threshold_mb", 15) * 1024 * 1024
        exclude_labels = [l.lower() for l in rules.get("exclude_labels", [])]
        exclude_genres = [g.lower() for g in rules.get("exclude_genres", [])]

        filtered: list[dict[str, Any]] = []
        for item in items:
            path = item.get("file_path", "")
            size = item.get("original_size", 0)

            if not path or not os.path.exists(path):
                continue
            if size < dummy_threshold_bytes:
                continue

            # 1. Check exclusions
            item_labels = [l.lower() for l in item.get("labels", [])]
            item_genres = [g.lower() for g in item.get("genres", [])]

            if any(l in item_labels for l in exclude_labels):
                logger.debug("Skipping '%s' due to excluded label", item.get("title"))
                continue

            if any(g in item_genres for g in exclude_genres):
                logger.debug("Skipping '%s' due to excluded genre", item.get("title"))
                continue

            # 2. Check watch age (retention grace period)
            last_watched = item.get("last_watched")
            if min_age_days > 0:
                if not last_watched:
                    # If we don't know when it was watched, skip it for safety
                    logger.debug("Skipping '%s' because watch date is unknown", item.get("title"))
                    continue
                if last_watched.tzinfo is not None:
                    cutoff = datetime.now(timezone.utc)
                else:
                    cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
                if last_watched > cutoff - timedelta(days=min_age_days):
                    logger.debug("Skipping '%s' because it was watched recently (%s)", item.get("title"), last_watched)
                    continue

            filtered.append(item)

        return filtered

    def scan(self) -> dict[str, Any]:
        """Dry-run scan: report what would be archived without touching anything."""
        report: dict[str, Any] = {"servers": {}, "total_savings_gb": 0.0, "total_items": 0}
        for server in self.servers:
            cfg = server.config
            libs = cfg.get("libraries", [])
            items = server.get_watched_items(libs)
            filtered = self._filter_items(items)
            saved = sum(
                item.get("original_size", 0) for item in filtered
            ) - sum(
                len(base64.b64decode(DUMMY_VIDEOS.get(
                    os.path.splitext(item.get("file_path", ""))[1].lower(), ""
                ) or "AA=="))
                for item in filtered
            )
            server_name = f"{server.server_type} ({cfg.get('url', '?')})"
            report["servers"][server_name] = {
                "watched_found": len(items),
                "candidates": len(filtered),
                "estimated_savings_gb": saved / (1024**3),
            }
            report["total_items"] += len(filtered)
            report["total_savings_gb"] += saved / (1024**3)

        return report

    def archive(self, dry_run: bool = False) -> dict[str, Any]:
        """Run the full archival process."""
        safety_cfg = self.config.get("safety", {})
        allow_auto = safety_cfg.get("allow_automated_archival", False)
        if not dry_run and not allow_auto:
            logger.warning(
                "Automated file deletion/archiving is disabled (safety.allow_automated_archival is Off). Forcing DRY-RUN simulation mode."
            )
            dry_run = True

        results: dict[str, Any] = {"archived": [], "errors": [], "skipped": []}

        for server in self.servers:
            cfg = server.config
            libs = cfg.get("libraries", [])
            logger.info(
                "Processing %s server: %s", server.server_type, cfg["url"]
            )

            items = server.get_watched_items(libs)
            filtered = self._filter_items(items)

            for item in filtered:
                item_id = item["id"]
                title = item["title"]
                file_path = item["file_path"]
                original_size = item["original_size"]
                media_type = item["type"]
                ext = os.path.splitext(file_path)[1].lower()

                if self.db.item_exists(server.server_type, item_id):
                    logger.debug("Already archived: %s", title)
                    results["skipped"].append(title)
                    continue

                dummy_base64 = DUMMY_VIDEOS.get(ext)
                if not dummy_base64:
                    logger.warning(
                        "No dummy template for extension '%s' — skipping %s",
                        ext,
                        title,
                    )
                    results["skipped"].append(title)
                    continue

                gb_saved = (original_size - 20000) / (1024**3)  # approx

                if dry_run:
                    logger.info("[DRY-RUN] Would archive: %s (%.2f GB)", title, gb_saved)
                    results["archived"].append(title)
                    continue

                logger.info("Archiving: %s (%.2f GB)", title, gb_saved)
                backup_poster_path: str | None = None
                backup_media_path: str | None = None
                poster_success = False

                try:
                    # 1. Download poster
                    poster_tmp = f"/tmp/mediaspektor_poster_{item_id}.jpg"
                    if server.download_poster(item_id, poster_tmp):
                        # Backup original poster
                        poster_backup = (
                            self.backup_dir
                            / f"{server.server_type}_{item_id}_poster_original.jpg"
                        )
                        shutil.copy2(poster_tmp, str(poster_backup))
                        backup_poster_path = str(poster_backup)

                        # Apply overlay
                        poster_overlay = (
                            self.backup_dir
                            / f"{server.server_type}_{item_id}_poster_overlay.jpg"
                        )
                        self.overlay.apply_overlay(
                            poster_tmp, str(poster_overlay), gb_saved
                        )

                        # Upload modified poster
                        if server.upload_poster(item_id, str(poster_overlay)):
                            poster_success = True
                        else:
                            raise RuntimeError("Failed to upload poster")

                        # Clean up tmp
                        os.unlink(poster_tmp)
                    else:
                        logger.warning(
                            "No poster for %s — skipping overlay", title
                        )

                    # 2-3. Backup (if configured) + safely swap in the dummy file.
                    # Pre-flight checks abort before any deletion, so a failure
                    # (bad path / permissions) never destroys the original.
                    dummy_bytes = base64.b64decode(dummy_base64)
                    backup_target = (
                        str(self.backup_dir / f"{server.server_type}_{item_id}{ext}")
                        if self.config.get("safety", {}).get("backup_original_media", False)
                        else None
                    )
                    backup_media_path = self._replace_with_dummy(
                        file_path, dummy_bytes, backup_target
                    )

                    # 4. Log to database
                    self.db.insert(
                        server_type=server.server_type,
                        server_item_id=item_id,
                        title=title,
                        media_type=media_type,
                        original_path=file_path,
                        original_size_bytes=original_size,
                        dummy_size_bytes=len(dummy_bytes),
                        backup_poster_path=backup_poster_path,
                        backup_media_path=backup_media_path,
                        status="archived",
                    )

                    # 5. Unmonitor in *Arr
                    if media_type == "movie" and self.radarr:
                        self.radarr.unmonitor_movie_by_path(file_path, item.get("external_ids"))
                    elif media_type == "episode" and self.sonarr:
                        self.sonarr.unmonitor_episode_by_path(file_path)

                    results["archived"].append(title)

                except Exception as exc:
                    logger.error(
                        "Failed to archive '%s': %s — rolling back", title, exc
                    )
                    # Rollback: restore poster if uploaded
                    if poster_success and backup_poster_path:
                        try:
                            server.upload_poster(item_id, backup_poster_path)
                        except Exception as rb_exc:
                            logger.error(
                                "Rollback poster failed: %s", rb_exc
                            )
                    results["errors"].append({"title": title, "error": str(exc)})

            # Trigger scan after processing all items for this server
            if results["archived"] and not dry_run:
                server.trigger_library_scan()

        return results

    def restore(self, server_type: str, item_id: str) -> bool:
        """Restore a single archived item and all sibling rows across servers."""
        record = self.db.get_item(server_type, item_id)
        if not record:
            logger.error("No archived record for %s/%s", server_type, item_id)
            return False

        logger.info("Restoring: %s (fanning out to sibling rows)", record["title"])

        siblings = self.db.get_items_by_path(record["original_path"], status="archived")
        if not siblings:
            siblings = [record]

        # Restore media file once (from first sibling that has it)
        media_restored = False
        for sib in siblings:
            if sib.get("backup_media_path") and os.path.exists(sib["backup_media_path"]):
                shutil.move(sib["backup_media_path"], record["original_path"])
                logger.info("Restored media file to %s", record["original_path"])
                media_restored = True
                break
        if not media_restored:
            logger.warning(
                "Original media backup not found — "
                "please manually restore the file to: %s",
                record["original_path"],
            )

        # Fan out poster restore and status update to every sibling connector
        for sib in siblings:
            srv_type = sib["server_type"]
            srv_item_id = sib["server_item_id"]
            server = None
            for s in self.servers:
                if s.server_type == srv_type:
                    server = s
                    break
            if not server:
                logger.warning("No active %s server configured — cannot restore poster for %s", srv_type, sib.get("title"))
                self.db.update_status(srv_type, srv_item_id, "restored")
                continue

            backup_poster = sib.get("backup_poster_path")
            if backup_poster and os.path.exists(backup_poster):
                try:
                    server.upload_poster(srv_item_id, backup_poster)
                    logger.info("Restored poster for %s on %s", sib.get("title"), srv_type)
                except Exception as exc:
                    logger.error("Failed to restore poster for %s on %s: %s", sib.get("title"), srv_type, exc)
            try:
                server.trigger_library_scan(media_type=sib.get("media_type"), item_id=srv_item_id)
            except Exception as exc:
                logger.warning("Library scan failed for %s: %s", srv_type, exc)

            self.db.update_status(srv_type, srv_item_id, "restored")

        return True

    def regenerate_item(self, server_type: str, item_id: str, target: str) -> dict[str, Any]:
        """Regenerate the poster or dummy video for an already archived item."""
        results = {"success": False, "messages": []}
        db_item = self.db.get_item(server_type, item_id)
        if not db_item:
            results["error"] = "Item not found in database."
            return results

        title = db_item.get("title", "Unknown")
        original_size = db_item.get("original_size_bytes", 0)
        dummy_size = db_item.get("dummy_size_bytes", 0)
        gb_saved = max(0, original_size - dummy_size) / (1024 ** 3)
        media_type = db_item.get("media_type", "movie")

        if target == "poster":
            backup_poster = db_item.get("backup_poster_path")
            if not backup_poster or not os.path.exists(backup_poster):
                results["error"] = "Original poster backup not found."
                return results

            # Find siblings in the database that are currently archived at the same path
            siblings = self.db.get_items_by_path(db_item.get("original_path"), status="archived")
            if not siblings:
                siblings = [{
                    "server_type": server_type,
                    "server_item_id": item_id,
                    "title": title
                }]

            # Re-apply the real saved amount: use the largest saving recorded across
            # all of this movie's rows. The clicked server's row can hold a stale/zero
            # original size while a sibling kept the true value — prefer the real one.
            for sib in siblings:
                sib_saved = max(0, sib.get("original_size_bytes", 0) - sib.get("dummy_size_bytes", 0))
                if sib_saved > (original_size - dummy_size):
                    original_size, dummy_size = sib.get("original_size_bytes", 0), sib.get("dummy_size_bytes", 0)
            gb_saved = max(0, original_size - dummy_size) / (1024 ** 3)

            success_count = 0
            for sib in siblings:
                srv_type = sib["server_type"]
                srv_id = sib["server_item_id"]
                server = None
                for s in self.servers:
                    if s.server_type == srv_type:
                        server = s
                        break
                
                if not server:
                    continue

                poster_overlay = self.backup_dir / f"{srv_type}_{srv_id}_poster_overlay.jpg"
                if self.overlay.apply_overlay(backup_poster, str(poster_overlay), gb_saved):
                    if server.upload_poster(srv_id, str(poster_overlay)):
                        success_count += 1
                        results["messages"].append(f"Regenerated poster for {srv_type}")
                    else:
                        results["messages"].append(f"Failed to upload poster to {srv_type}")
                else:
                    results["messages"].append(f"Failed to apply overlay for {srv_type}")

            results["success"] = success_count > 0
            return results

        elif target == "video":
            original_path = db_item.get("original_path")
            if not original_path:
                results["error"] = "Original path not found in database."
                return results

            # Data-safety guard: only regenerate the dummy for an item that is
            # genuinely archived AND whose on-disk file is still a tiny dummy.
            # _replace_with_dummy(backup_target=None) overwrites in place with no
            # backup, so if the DB and disk ever drift (e.g. a restored file still
            # flagged archived) this would destroy real media irrecoverably.
            if db_item.get("status") != "archived":
                results["error"] = (
                    f"Refusing to regenerate video — item status is "
                    f"'{db_item.get('status')}', not 'archived'."
                )
                return results

            SAFE_DUMMY_CEILING = 50 * 1024 * 1024  # 50 MB — no dummy is this big
            try:
                on_disk = os.path.getsize(original_path)
            except OSError as exc:
                results["error"] = f"Cannot stat file at '{original_path}': {exc}"
                return results
            if on_disk > SAFE_DUMMY_CEILING:
                results["error"] = (
                    f"Refusing to overwrite '{original_path}' ({on_disk / (1024**2):.0f} MB) "
                    f"— file is too large to be a dummy, it looks like real media."
                )
                logger.error(results["error"])
                return results

            ext = os.path.splitext(original_path)[1].lower()
            dummy_b64 = DUMMY_VIDEOS.get(ext) or DUMMY_VIDEOS.get(".mp4")
            dummy_bytes = base64.b64decode(dummy_b64)

            try:
                self._replace_with_dummy(original_path, dummy_bytes, backup_target=None)
                results["success"] = True
                results["messages"].append("Dummy video regenerated and permissions applied.")
                logger.info("Regenerated dummy video for '%s'", title)
            except Exception as exc:
                results["error"] = f"Failed to regenerate video: {exc}"
                logger.error(results["error"])
                return results

            # Trigger a scoped refresh for each server that actually has this item,
            # limited to that item / its library type — not a full scan of every server.
            for sib in self.db.get_items_by_path(original_path, status="archived"):
                server = next((s for s in self.servers if s.server_type == sib["server_type"]), None)
                if not server:
                    continue
                try:
                    server.trigger_library_scan(media_type=sib.get("media_type"), item_id=sib["server_item_id"])
                except Exception as exc:
                    logger.warning("Library scan failed on %s: %s", sib["server_type"], exc)

            return results

        results["error"] = "Invalid regenerate target."
        return results

    def stats(self) -> dict[str, Any]:
        return self.db.get_stats()

    def get_item_monitor(self, server_type: str, item_id: str) -> dict[str, Any]:
        """Report the current *Arr monitored state for an archived item, or why it's
        unavailable (so the UI can show/hide the toggle)."""
        rec = self.db.get_item(server_type, item_id)
        if not rec:
            return {"available": False, "reason": "Item not found in database."}
        mtype = rec.get("media_type", "movie")
        fp = rec.get("original_path")
        if mtype == "movie":
            if not self.radarr:
                return {"available": False, "reason": "Radarr is not configured."}
            state = self.radarr.get_movie_monitored(fp)
            arr = "Radarr"
        else:
            if not self.sonarr:
                return {"available": False, "reason": "Sonarr is not configured."}
            state = self.sonarr.get_episode_monitored(fp)
            arr = "Sonarr"
        if state is None:
            return {"available": False, "reason": f"No matching item found in {arr}."}
        return {"available": True, "monitored": state, "arr": arr}

    def set_item_monitor(self, server_type: str, item_id: str, monitored: bool) -> dict[str, Any]:
        """Set the *Arr monitored flag for an archived item. Re-monitoring lets the
        *Arr fetch the real (larger) file again; unmonitoring stops re-downloads."""
        rec = self.db.get_item(server_type, item_id)
        if not rec:
            return {"success": False, "error": "Item not found in database."}
        mtype = rec.get("media_type", "movie")
        fp = rec.get("original_path")
        if mtype == "movie":
            if not self.radarr:
                return {"success": False, "error": "Radarr is not configured."}
            ok = self.radarr.set_movie_monitored(fp, monitored)
            arr = "Radarr"
        else:
            if not self.sonarr:
                return {"success": False, "error": "Sonarr is not configured."}
            ok = self.sonarr.set_episode_monitored(fp, monitored)
            arr = "Sonarr"
        if not ok:
            return {"success": False, "error": f"No matching item found in {arr} (check path/IDs)."}
        return {"success": True, "monitored": monitored, "arr": arr}

    def _replace_with_dummy(
        self, file_path: str, dummy_bytes: bytes, backup_target: str | None = None
    ) -> str | None:
        """Safely swap the original media file for the dummy.

        Verifies the original exists and its directory is writable BEFORE any
        destructive action, then writes the dummy to a temp file and atomically
        replaces the original — so a failure never leaves the file missing.
        Returns the backup media path (when backing up) or None.
        """
        directory = os.path.dirname(file_path) or "."
        if not os.path.isfile(file_path):
            raise FileNotFoundError(
                f"Original media not found at '{file_path}'. MediaSpektor cannot reach the file "
                f"— ensure the media share is mounted at the same path the server reports "
                f"(e.g. /data) with read/write access."
            )
        if not os.access(directory, os.W_OK):
            raise PermissionError(
                f"Directory '{directory}' is not writable by MediaSpektor — check the "
                f"container/user permissions and the volume mount."
            )

        # Clone the original file's permissions (and, if privileged, ownership)
        # onto the dummy. mkstemp creates 0600 files and os.replace preserves that,
        # which would lock a media server running as a different user out of the
        # dummy — making it drop the item instead of rescanning it.
        orig_stat = os.stat(file_path)

        backup_media_path: str | None = None
        if backup_target:
            shutil.move(file_path, backup_target)
            backup_media_path = backup_target

        # Atomic replace: write dummy to a temp file in the same dir, then os.replace.
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".ms-tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(dummy_bytes)
            try:
                os.chmod(tmp_path, orig_stat.st_mode & 0o777)
            except OSError as exc:
                logger.debug("Could not clone permissions onto dummy: %s", exc)
            try:
                os.chown(tmp_path, orig_stat.st_uid, orig_stat.st_gid)
            except OSError as exc:
                logger.debug("Could not clone ownership onto dummy (unprivileged — fine): %s", exc)
            os.replace(tmp_path, file_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        return backup_media_path

    def archive_item(self, server_type: str, item_id: str) -> dict[str, Any]:
        """Archive a single chosen movie or episode, propagating to all servers."""
        results: dict[str, Any] = {"success": False, "error": None, "warnings": []}

        # 1. Find source server
        source = None
        for s in self.servers:
            if s.server_type == server_type:
                source = s
                break

        if not source:
            results["error"] = f"No active {server_type} server configured."
            logger.error(results["error"])
            return results

        try:
            # 2. Get item metadata
            item = source.get_item_metadata(item_id)
            if not item:
                raise ValueError("Item metadata could not be retrieved from server.")

            title = item["title"]
            file_path = item["file_path"]
            original_size = item["original_size"]
            media_type = item["type"]
            ext = os.path.splitext(file_path)[1].lower()

            if self.db.item_exists(server_type, item_id):
                results["error"] = f"Item '{title}' is already archived."
                logger.warning(results["error"])
                return results

            dummy_base64 = DUMMY_VIDEOS.get(ext)
            if not dummy_base64:
                raise ValueError(f"No dummy template for extension '{ext}'")

            dummy_bytes = base64.b64decode(dummy_base64)
            gb_saved = (original_size - 20000) / (1024**3)

            # SAFETY GATE — honor the Dry-Run switch for the single-item
            # (UI-triggered) archive. Without this the "Confirm Spektor" button
            # irreversibly swaps the real media file even with Dry-Run enabled,
            # which has destroyed real files. (allow_automated_archival is NOT
            # gated here — that switch governs the scheduled/bulk run; a manual
            # click is a deliberate, explicit action.)
            if self.config.get("safety", {}).get("dry_run", False):
                logger.info("[DRY-RUN] Would Spektor: %s (%.2f GB) — no files changed.", title, gb_saved)
                results["success"] = True
                results["dry_run"] = True
                results["error"] = None
                results["warnings"].append(
                    f"Dry-Run is enabled: simulated archiving '{title}' ({gb_saved:.2f} GB). No files were modified."
                )
                return results

            logger.info("Spektoring single item: %s (%.2f GB) — propagating to all servers", title, gb_saved)

            # Expand external IDs once (noop without TMDB key; movies only).
            expanded_ids = self._expand_external_ids(media_type, item["external_ids"])

            # PHASE 1 — upload the badged poster to every matched server FIRST,
            # while each item record is still stable. Swapping the file first would
            # trip the server's inotify rescan and invalidate the record mid-upload,
            # causing 404/400/500 on the poster API.
            targets: list[dict[str, Any]] = []
            for server in self.servers:
                target = item if server is source else server.find_item(file_path, expanded_ids, media_type)
                if target is None:
                    if server is not source:
                        logger.warning("No %s match for '%s' — skipping poster", server.server_type, title)
                        results["warnings"].append(f"Skipped {server.server_type}: no match found")
                    continue

                local_id = target["id"]
                local_type = server.server_type
                backup_poster_path: str | None = None
                try:
                    poster_tmp = f"/tmp/mediaspektor_poster_{local_type}_{local_id}.jpg"
                    if server.download_poster(local_id, poster_tmp):
                        poster_backup = self.backup_dir / f"{local_type}_{local_id}_poster_original.jpg"
                        shutil.copy2(poster_tmp, str(poster_backup))
                        backup_poster_path = str(poster_backup)

                        poster_overlay = self.backup_dir / f"{local_type}_{local_id}_poster_overlay.jpg"
                        self.overlay.apply_overlay(poster_tmp, str(poster_overlay), gb_saved)

                        if not server.upload_poster(local_id, str(poster_overlay)):
                            logger.warning("Failed to upload poster to %s for %s", local_type, title)

                        if os.path.exists(poster_tmp):
                            os.unlink(poster_tmp)
                except Exception as exc:
                    logger.warning("Poster processing failed for %s on %s: %s", title, local_type, exc)

                targets.append({"server": server, "local_id": local_id, "backup_poster_path": backup_poster_path})

            # PHASE 2 — swap the physical file exactly once. Pre-flight checks abort
            # before any delete; on failure, roll the posters back to the originals.
            backup_target = (
                str(self.backup_dir / f"{server_type}_{item_id}{ext}")
                if self.config.get("safety", {}).get("backup_original_media", False)
                else None
            )
            try:
                backup_media_path = self._replace_with_dummy(file_path, dummy_bytes, backup_target)
            except Exception as exc:
                logger.error("File swap failed for '%s': %s — restoring posters", title, exc)
                for t in targets:
                    bp = t["backup_poster_path"]
                    if bp and os.path.exists(bp):
                        try:
                            t["server"].upload_poster(t["local_id"], bp)
                        except Exception as rb_exc:
                            logger.error("Poster rollback failed on %s: %s", t["server"].server_type, rb_exc)
                results["error"] = str(exc)
                return results

            # PHASE 3 — record state, trigger scans, and unmonitor in *Arr now that
            # the dummy is safely in place.
            for t in targets:
                server = t["server"]
                self.db.insert(
                    server_type=server.server_type,
                    server_item_id=t["local_id"],
                    title=title,
                    media_type=media_type,
                    original_path=file_path,
                    original_size_bytes=original_size,
                    dummy_size_bytes=len(dummy_bytes),
                    backup_poster_path=t["backup_poster_path"],
                    backup_media_path=backup_media_path if server is source else None,
                    status="archived",
                )
                try:
                    server.trigger_library_scan(media_type=media_type, item_id=t["local_id"])
                except Exception as exc:
                    logger.warning("Library scan failed for %s: %s", server.server_type, exc)

            # PHASE 4 — re-apply the badged poster LAST. The file swap makes Jellyfin/Emby
            # re-extract the Primary image from the dummy video, clobbering the PHASE-1 upload;
            # uploading again after the swap+scan makes our badge the final image.
            for t in targets:
                srv = t["server"]
                if srv.server_type not in ("jellyfin", "emby"):
                    continue
                overlay = self.backup_dir / f"{srv.server_type}_{t['local_id']}_poster_overlay.jpg"
                if not overlay.exists():
                    continue
                try:
                    time.sleep(2)  # let the swap-triggered scan settle first
                    srv.upload_poster(t["local_id"], str(overlay))
                    logger.info("Re-applied badged poster on %s for item %s", srv.server_type, t["local_id"])
                except Exception as exc:
                    logger.warning("Poster re-apply failed on %s: %s", srv.server_type, exc)

            if media_type == "movie" and self.radarr:
                self.radarr.unmonitor_movie_by_path(file_path, expanded_ids)
            elif media_type == "episode" and self.sonarr:
                self.sonarr.unmonitor_episode_by_path(file_path)

            results["success"] = True
            logger.info("Successfully archived and 'Spektored' item: %s", title)
            return results

        except Exception as exc:
            logger.error("Failed to archive item %s: %s", item_id, exc)
            results["error"] = str(exc)
            return results

    def bulk_episode_plan(self, server_type: str, show_id: str, season_id: str | None = None) -> dict[str, Any]:
        """List episodes that a season/series bulk-Spektor would archive (those not already
        archived), with counts and total size for the confirmation dialog."""
        server = next((s for s in self.servers if s.server_type == server_type), None)
        if not server:
            return {"error": f"No active {server_type} server."}
        seasons = [{"id": season_id}] if season_id else server.get_seasons(show_id)
        to_do, unwatched, total = [], 0, 0
        already = 0
        for sea in seasons:
            for ep in server.get_episodes(show_id, sea["id"]):
                if self.db.item_exists(server_type, str(ep["id"])):
                    already += 1
                    continue
                to_do.append(str(ep["id"]))
                total += ep.get("original_size", 0) or 0
                if not ep.get("is_watched", False):
                    unwatched += 1
        return {"item_ids": to_do, "count": len(to_do), "unwatched": unwatched,
                "already_archived": already, "total_size_bytes": total}

    def bulk_archive(self, server_type: str, item_ids: list[str]) -> dict[str, Any]:
        """Archive each episode id via archive_item (honors Dry-Run, propagation, etc.)."""
        results = {"archived": [], "errors": []}
        for iid in item_ids:
            res = self.archive_item(server_type, str(iid))
            if res.get("success"):
                results["archived"].append(iid)
            else:
                results["errors"].append({"item_id": iid, "error": res.get("error")})
        return results


# ---------------------------------------------------------------------------
# FastAPI Web Server
# ---------------------------------------------------------------------------
import collections

class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.logs = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            log_entry = self.format(record)
            self.logs.append(log_entry)
        except Exception:
            self.handleError(record)

memory_log_handler = MemoryLogHandler()
memory_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
memory_log_handler.setLevel(logging.INFO)
# Attach only to the root logger. The "mediaspektor" logger propagates to root,
# so adding the same handler to both captured every record twice (doubled lines
# in the dashboard activity log). Root alone captures mediaspektor + libraries once.
logging.getLogger().addHandler(memory_log_handler)

# Initialize FastAPI App
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Response, Depends
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uuid

app = FastAPI(title="MediaSpektor", description="Modern self-hosted watch state storage archiver")

# Authentication session memory
VALID_SESSIONS: set[str] = set()

def _invalidate_other_sessions(keep: str | None = None) -> None:
    """Drop every active session except `keep` (the caller's), so a credential
    change immediately logs out anyone else holding a live cookie."""
    VALID_SESSIONS.clear()
    if keep:
        VALID_SESSIONS.add(keep)

def verify_auth(request: Request):
    spektor = get_spektor()
    security_config = spektor.config.get("security", {})
    if not security_config.get("enabled", False):
        return
    
    session_cookie = request.cookies.get("mediaspektor_session")
    if not session_cookie or session_cookie not in VALID_SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")

# No CORS middleware: the dashboard and API are served same-origin, so cross-origin
# access isn't needed. (A wildcard allow_origins with allow_credentials is both
# rejected by browsers and a security smell, so it is intentionally omitted.)

# Global variables for Orchestrator and config path
GLOBAL_SPEKTOR: MediaSpektor | None = None
CONFIG_PATH: str = "config.yaml"

def get_spektor() -> MediaSpektor:
    global GLOBAL_SPEKTOR
    if GLOBAL_SPEKTOR is None:
        GLOBAL_SPEKTOR = MediaSpektor(CONFIG_PATH)
    return GLOBAL_SPEKTOR

# Static files mapping
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    return FileResponse("static/index.html")

class LoginReq(BaseModel):
    username: str
    password: str

def _cookie_secure(security_config: dict) -> bool:
    """Whether the session cookie should carry the Secure flag.

    Driven by an explicit config flag, NOT the request scheme. With a reverse
    proxy in front, X-Forwarded-Proto is attacker-spoofable, so deriving Secure
    from request.url.scheme could be tricked off. Operators serving over HTTPS
    set security.https_only: true.
    """
    return bool(security_config.get("https_only", False))


@app.post("/api/login")
def api_login(req: LoginReq, request: Request, response: Response):
    spektor = get_spektor()
    security_config = spektor.config.get("security", {})
    if not security_config.get("enabled", False):
        return {"success": True}

    config_user = security_config.get("username", "admin")
    config_pass = security_config.get("password", "admin")

    # Constant-time comparison to avoid leaking credential length/content via timing.
    user_ok = hmac.compare_digest(req.username.encode(), config_user.encode())
    pass_ok = hmac.compare_digest(req.password.encode(), config_pass.encode())
    if user_ok and pass_ok:
        session_id = str(uuid.uuid4())
        VALID_SESSIONS.add(session_id)
        response.set_cookie(
            key="mediaspektor_session",
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(security_config),
            max_age=30 * 24 * 60 * 60  # 30 days
        )
        # Surface whether the default password is still in use so the UI can force a change.
        return {"success": True, "must_change_password": config_pass in ("", "admin")}
    else:
        raise HTTPException(status_code=401, detail="Invalid username or password")

@app.post("/api/logout")
def api_logout(response: Response, request: Request):
    session_cookie = request.cookies.get("mediaspektor_session")
    if session_cookie in VALID_SESSIONS:
        VALID_SESSIONS.remove(session_cookie)
    response.delete_cookie(key="mediaspektor_session")
    return {"success": True}

@app.get("/api/config", dependencies=[Depends(verify_auth)])
def get_config():
    spektor = get_spektor()
    return spektor.config

class UpdateConfigReq(BaseModel):
    config: dict

@app.post("/api/config", dependencies=[Depends(verify_auth)])
def update_config(req: UpdateConfigReq, request: Request):
    global GLOBAL_SPEKTOR
    try:
        old_sec = (get_spektor().config or {}).get("security", {}) or {}
        new_cfg = dict(req.config)
        # Never let the settings form silently drop the security block — that would
        # disable authentication. Preserve the existing one if the client omits it.
        if "security" not in new_cfg:
            existing = (get_spektor().config or {}).get("security")
            if existing:
                new_cfg["security"] = existing
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(new_cfg, f)
        GLOBAL_SPEKTOR = MediaSpektor(CONFIG_PATH)
        # If the dashboard credentials changed, revoke every other live session
        # (keep the caller's) so an old/leaked cookie can't outlive the change.
        new_sec = new_cfg.get("security", {}) or {}
        if (
            old_sec.get("username") != new_sec.get("username")
            or old_sec.get("password") != new_sec.get("password")
        ):
            _invalidate_other_sessions(keep=request.cookies.get("mediaspektor_session"))
            logger.info("Dashboard credentials changed via config — other sessions invalidated.")
        logger.info("Configuration updated and reloaded successfully.")
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class ChangePasswordReq(BaseModel):
    password: str
    username: str | None = None


@app.post("/api/change-password", dependencies=[Depends(verify_auth)])
def change_password(req: ChangePasswordReq, request: Request):
    global GLOBAL_SPEKTOR
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if req.password in ("", "admin"):
        raise HTTPException(status_code=400, detail="Choose a password other than the default.")
    spektor = get_spektor()
    cfg = spektor.config
    sec = cfg.setdefault("security", {})
    sec["enabled"] = True
    if req.username:
        sec["username"] = req.username
    sec["password"] = req.password
    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(cfg, f)
        GLOBAL_SPEKTOR = MediaSpektor(CONFIG_PATH)
        # Credentials changed — revoke every other live session, keeping only
        # the caller's so they stay logged in after the change.
        _invalidate_other_sessions(keep=request.cookies.get("mediaspektor_session"))
        logger.info("Dashboard password updated; other sessions invalidated.")
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/stats", dependencies=[Depends(verify_auth)])
def get_web_stats():
    spektor = get_spektor()
    return spektor.stats()

@app.get("/api/logs", dependencies=[Depends(verify_auth)])
def get_web_logs():
    return list(memory_log_handler.logs)

@app.get("/api/movies", dependencies=[Depends(verify_auth)])
def get_web_movies():
    spektor = get_spektor()
    # Dedupe the same physical movie across servers sharing one library.
    # Key on file_path (servers mount the library at the same path), falling
    # back to title+year. Keep one card; prefer one already shown as archived.
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for server in spektor.servers:
        libs = server.config.get("libraries", [])
        for m in server.get_movies(libs):
            db_item = spektor.db.get_item(server.server_type, m["id"])
            m["status"] = db_item["status"] if db_item else "original"
            m["server_type"] = server.server_type
            key = m.get("file_path") or f"{m.get('title', '')}|{m.get('year', '')}"
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = m
                order.append(key)
            elif existing["status"] != "archived" and m["status"] == "archived":
                by_key[key] = m
    return [by_key[k] for k in order]

@app.get("/api/shows", dependencies=[Depends(verify_auth)])
def get_web_shows():
    spektor = get_spektor()
    # Dedupe the same show across servers (shows have no file_path; key on title+year).
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for server in spektor.servers:
        libs = server.config.get("libraries", [])
        for s in server.get_shows(libs):
            s["server_type"] = server.server_type
            key = f"{s.get('title', '')}|{s.get('year', '')}"
            if key not in by_key:
                by_key[key] = s
                order.append(key)
                cache_key = (server.server_type, s["id"])
                if cache_key not in _SHOW_SIZE_CACHE:
                    try:
                        _SHOW_SIZE_CACHE[cache_key] = server.get_show_total_size(s["id"])
                    except Exception:
                        _SHOW_SIZE_CACHE[cache_key] = 0
                s["total_size"] = _SHOW_SIZE_CACHE[cache_key]
    return [by_key[k] for k in order]

@app.get("/api/shows/{server_type}/{show_id}/seasons", dependencies=[Depends(verify_auth)])
def get_web_seasons(server_type: str, show_id: str):
    spektor = get_spektor()
    for server in spektor.servers:
        if server.server_type == server_type:
            return server.get_seasons(show_id)
    raise HTTPException(status_code=404, detail="Server type not found or not active")

@app.get("/api/shows/{server_type}/{show_id}/seasons/{season_id}/episodes", dependencies=[Depends(verify_auth)])
def get_web_episodes(server_type: str, show_id: str, season_id: str):
    spektor = get_spektor()
    for server in spektor.servers:
        if server.server_type == server_type:
            episodes = server.get_episodes(show_id, season_id)
            for ep in episodes:
                db_item = spektor.db.get_item(server_type, ep["id"])
                ep["status"] = db_item["status"] if db_item else "original"
            return episodes
    raise HTTPException(status_code=404, detail="Server type not found or not active")

@app.get("/api/posterproxy", dependencies=[Depends(verify_auth)])
def poster_proxy(server_type: str, item_id: str):
    spektor = get_spektor()
    server = None
    for s in spektor.servers:
        if s.server_type == server_type:
            server = s
            break
    if not server:
        raise HTTPException(status_code=404, detail=f"Active server connector '{server_type}' not found.")

    try:
        if server_type == "plex":
            parsed_id = int(item_id) if isinstance(item_id, str) and item_id.isdigit() else item_id
            item = server._server.fetchItem(parsed_id)
            if not item.posterUrl:
                raise HTTPException(status_code=404, detail="Plex poster url missing")
            url = item.posterUrl
            if url.startswith("/"):
                url = server.config["url"].rstrip("/") + url + "?X-Plex-Token=" + server.config["token"]
            resp = HTTP.get(url, timeout=30)
        elif server_type in ("jellyfin", "emby"):
            url = urljoin(server.base_url + "/", f"Items/{item_id}/Images/Primary")
            resp = HTTP.get(url, headers=server.headers, timeout=30)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported server type '{server_type}'")
        resp.raise_for_status()
        # Read fully (posters are small) so the pooled connection is released
        # immediately instead of being held open by a streaming generator.
        return Response(
            content=resp.content,
            media_type=resp.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Poster proxy failed: {exc}")

class ActionReq(BaseModel):
    server_type: str
    item_id: str | int  # Plex ratingKeys arrive as JSON numbers; coerced to str at use

def run_bg_spektor(server_type: str, item_id: str):
    spektor = get_spektor()
    spektor.archive_item(server_type, item_id)

def run_bg_restore(server_type: str, item_id: str):
    spektor = get_spektor()
    spektor.restore(server_type, item_id)

@app.post("/api/spektor", dependencies=[Depends(verify_auth)])
def trigger_spektor(req: ActionReq, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(run_bg_spektor, req.server_type, str(req.item_id))
    return {"success": True, "message": "Archival process queued as background task."}

@app.get("/api/shows/{server_type}/{show_id}/plan", dependencies=[Depends(verify_auth)])
def bulk_plan(server_type: str, show_id: str, season_id: str | None = None):
    return get_spektor().bulk_episode_plan(server_type, show_id, season_id)

class BulkSpektorReq(BaseModel):
    server_type: str
    show_id: str
    season_id: str | None = None

def run_bg_bulk(server_type: str, show_id: str, season_id: str | None):
    spektor = get_spektor()
    plan = spektor.bulk_episode_plan(server_type, show_id, season_id)
    spektor.bulk_archive(server_type, plan.get("item_ids", []))

@app.post("/api/spektor-bulk", dependencies=[Depends(verify_auth)])
def trigger_bulk(req: BulkSpektorReq, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(run_bg_bulk, req.server_type, req.show_id, req.season_id)
    return {"success": True, "message": "Bulk archival queued as background task."}

@app.post("/api/restore", dependencies=[Depends(verify_auth)])
def trigger_restore(req: ActionReq, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(run_bg_restore, req.server_type, str(req.item_id))
    return {"success": True, "message": "Restoration process queued as background task."}

class RegenerateReq(BaseModel):
    server_type: str
    item_id: str | int
    target: str  # "poster" or "video"

def run_bg_regenerate(server_type: str, item_id: str, target: str):
    spektor = get_spektor()
    logger.info("Starting regeneration (%s) for %s item %s", target, server_type, item_id)
    try:
        results = spektor.regenerate_item(server_type, item_id, target)
        if not results.get("success"):
            logger.error("Regeneration failed: %s", results.get("error", "Unknown error"))
        else:
            msg = ", ".join(results.get("messages", []))
            logger.info("Regeneration completed successfully. Messages: %s", msg)
    except Exception as exc:
        logger.error("Regeneration exception: %s", exc)

@app.post("/api/regenerate", dependencies=[Depends(verify_auth)])
def trigger_regenerate(req: RegenerateReq, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(run_bg_regenerate, req.server_type, str(req.item_id), req.target)
    return {"success": True, "message": f"Regeneration ({req.target}) queued as background task."}


@app.get("/api/monitor-state", dependencies=[Depends(verify_auth)])
def get_monitor_state(server_type: str, item_id: str):
    return get_spektor().get_item_monitor(server_type, str(item_id))


class MonitorReq(BaseModel):
    server_type: str
    item_id: str | int
    monitored: bool

@app.post("/api/monitor", dependencies=[Depends(verify_auth)])
def set_monitor(req: MonitorReq):
    return get_spektor().set_item_monitor(req.server_type, str(req.item_id), req.monitored)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MediaSpektor — Reclaim disk space by archiving watched media.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override config to force dry-run simulation",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Run a dry-run inspection and report space savings",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Run the full archival execution",
    )
    parser.add_argument(
        "--restore",
        nargs=2,
        metavar=("SERVER_TYPE", "ITEM_ID"),
        help="Restore a previously archived item",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Display archive statistics",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the web server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to run the web server on (default: 5000)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("mediaspektor").setLevel(logging.DEBUG)
    else:
        logging.getLogger("mediaspektor").setLevel(logging.INFO)

    global CONFIG_PATH
    CONFIG_PATH = args.config

    # Initialize orchestrator
    get_spektor()

    if args.scan or args.archive or args.restore or args.stats:
        ghost = get_spektor()
        if args.stats:
            stats = ghost.stats()
            print(json.dumps(stats, indent=2))
            return

        if args.restore:
            server_type, item_id = args.restore
            success = ghost.restore(server_type, item_id)
            if success:
                logger.info("Restore complete for %s/%s", server_type, item_id)
            else:
                logger.error("Restore failed")
                sys.exit(1)
            return

        dry_run = args.dry_run or (
            ghost.config.get("safety", {}).get("dry_run", False)
        )

        if args.scan:
            report = ghost.scan()
            print(json.dumps(report, indent=2))
            return

        if args.archive:
            results = ghost.archive(dry_run=dry_run)
            print(json.dumps(results, indent=2))
            if results["errors"]:
                sys.exit(1)
            return
    else:
        logger.info("Starting MediaSpektor Self-Hosted Web App...")
        import uvicorn
        # Only trust X-Forwarded-* headers from explicitly configured proxy IPs.
        # Defaulting to "*" would let any client spoof their scheme/address; an
        # operator behind a reverse proxy sets security.trusted_proxies to that
        # proxy's address (a single IP, comma list, or "*" if they accept the risk).
        trusted = get_spektor().config.get("security", {}).get("trusted_proxies")
        if trusted:
            uvicorn.run(
                app, host=args.host, port=args.port,
                proxy_headers=True, forwarded_allow_ips=trusted,
            )
        else:
            uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    main()
