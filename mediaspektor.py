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
    ".mp4": "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6666666666666666666666666666666666666666///vBcEMBHR5GeM/kBH+J7/7vfwgTgkexBxLPL9CUj/Ed8Xj/6DXDX6QlZUGK8WB6Bmfm0W+77//QKzkiXJH/P19EWvbEu7j9ddddddcLocEX7fnwIu6eTkcsf/6/jn/UcKh/oNEaPBC11fw7mgPW0IHOX7X1/XVeHcYlcTPxmKvgi/cfy//6rnQLfDuaNKP7tqE6666666666666666666666666666666666666666666666666666666666666///fDoS4JHsQSPYy9AR/g+/93f+Qb4JHsSx+X6AD7ZPtx4//grKAk5sR78n7kf+gO+TfqPrrrrrrrrrrrrpf1D/0CwcTATNyLr8d7111/XjjeQG5xHIDdAR8u//z1zsOzZkBubKAL+LdQrXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX///BcO4EZH2A7wv8c7/e7/IEDYFnUrBI9h0Pn/QQ/k6juRF//grOAPVNkVq/oQmZ+Md9R9ddddddddddddddddcLoYABC93d9/BMhyx/9fhN+etVw+pxgrEXdgJAR8f/He/+p+wWnwj4Cjf+iR6j666666666666666666666666666666666666666666666666666666///LDoS5YwJE8RXgR4UXoHz/k/uXj/G5YgRXo2HmEeFF/P+gEAD5P9QnXXXXXXXXXXXXXXXXXXXXX1zif+gWBLnNIC/wf//6pXPBbDTh9EUvx3tQnXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX//l4QCXAivRs4CLNkBHqfn9CUj/HOUdJy//wVw2eePfQOOfjHfUJ11111111111111111111111/mv/oFg6BM1gRr6/jvfn+fmENm+c1kAmjIep7xnlCtdddddddddddddddddddddddddddddddddddddddddddddddddf/+XhAKcBJ2xAR64Elst6DqIJ5/QPR/HOXl//4KzglGjyH/0B31HfUfXXXXXXXXXXXXXXXXXXXXXXXXX/P/0HxkAi38Xn5zP54TwVCEgeM94zyhWuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuv//ImwgEuBF2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t1/MaMaHr229tv/mcVqH+g0FrvgFQYVILvB8j+k64CF5wJzKoWxmHYwBIQABMBix1vCExpAbCL+8NVZgACAR3+v/J3//gwgAyduJG1fg4LC+F6g+RU/TMfo+D7gCQgAQXxrfBhBx+VkAIog/hkgX6aemn/nRUkl5jULr229tv/Sr9NMzCmSqCWuuuuuuuuuuuv9IfGmHAoMJuHdJjKKTP3PzwitQrXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXwt7Q+w2Eo+n3cJv/P6+SEH0KhOuuuuuuuuuuuuEmCT/Q/odDPfDAP9grDOO+xFIcHtt/tbW1tbW1tbW1imGB+i5H/UEtdddddddddddf//SDDgSmjAepJkhNpZf0v6KJvTCddddddddddddddddddddddddddddddddddddddde0P/7DYQBI+lXXAQv1Hv66f5H3qEa666666666664pgkHaLRUX4YBwwDYbC5c5c40oq/aaa2tra2tra2trHMLWU8PnhPD6glrrrrrrrrrrrr/pD+CDg6CVtOngIzfR76/LDTH666666666666666666666666666666666666666//aX2Gx2BH6s/f9fzwqPrrrrrrrrrrrrjmCL//NCaGGAcMA2GwuFH3wo++NAKT6ff2tra2tra2tra09RtddddddddddddeH//YbHYEfqzy6X6/nhUfXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXFPT/A7VzDtXNddddddddddddczBEO01CzCoHghWWmnpp6aemmIICRUfT6fX7pJNVaq1VqrVWqtVaq1WH//YKwsf+uv0wS1111111111111xTCFP8z5n1CNdddddddddddddddddddddddddddddddddddddcLMYAha+Pf4EKp57yEx3P1/rUbXXXXXXXXXXXXCzBJ//rVe/wwwDhgDDYZy54y0YgkhwefT77W1tbW1tbW1tZmGJaoJa6666666666668P7Q+wWjgQa7fX9aj666666666666666666666666666666666666668P/+w2EOHpPWKOmfSfWFNzUI111111111111/w/pNhwEHeAHHQMJZpBuq/wiZlY68APxmElUXMP9OAMNlv/AAmQrFE24nyjvwOPhOqusIubeBdryHmLlY4wvHqmnpp/+RGFtv002uPwwbwQ8KcMvQCHWCg0kC3y4AB7D/jDclNxaFoFcjhyklWBbv3+4AoQACO52AVVpK9+ACh2UgQtFFk7XtBW1x/Dw0QcCmHHsAvw6PJAx/p4LB3KUQpBWA834DpTC/l5hugK4QAEd7uAXqTV7gAqUlIFNTRrtr/8MAgtCue4cmyptv/xptcf/+gXDuABTOaQEILbmsYIyAfhHjbgAbytj6Nnch/aX2GzMU/gB1eEEswgz164120ITS74e5iIqmz71BHXXXXXXXXXXXX//w4cHcCFrsl2vr/LA0fRgBrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrxh8NPYbCQNmmf8MSYSfe+d9QnXXXXXXXXXXXXS6VPTHVaxwKIdqHUUN29/gAg5P12XX3ABHEmF7eyiX/CM75+d8lvACIQSnIKz2PBYsYJVWM20DjO/f4AJI9qgRQOwv/qGAIO+3Bf5HYLoP9wcAdTpyPx32oA2YVzvwbQSAFg+WKBg8vEQ/xuyeCwAGBFTh/hB3cikCHcADspAYtTMhBAZ6cAE+QpKKspT6CdP8IaVD4bwOLcUQYZti/n+EkRBAAQHEsth/8VMYweEEyADJjPIEnP6GeFLgH+flPzew1rhYyJAMy9dDspb4shidvFCdh4B3qLJsR5eYs5wf0yLWA7/CQGIyG+EJmwgADQIMSkeCQCZup2yOx9yjiMmAVY79Mcohz4GF2vO7vv+qiuAQ7MQMQ6wb8a7/8CW4eKb8rcPIadQmkPwWw61yA4wggg3m8GBpgolL/IsPSZQDqiOCwCAWrj/4ADvQdu6RpALEEuE3GvwAZu9WbAnQo/hzgMwWv6a3IpHwGgEEIAhVu8C+tjFhcoAjQRNZwda0bFaQy/INwAxbHU3dMAN97X2R/KiD+mvRlDKxB9fodqHUUN28H8miXMzPgfwjwYQABsJYwI6lrKlOusAGBHtcJFv+SWh526DGX9/3gs+xIALSc/11qsf+ABBbk/XZA4YAL9kwveRRDXr4jO9Pzu5LeAjhOsF2H+8CCBCwYpr1zb8KBpgGFyOIDQ5tfLsX5FKwPDNDkgAqJ+f/AMHQdu44AOp04j82o7gNmFc78Cef4Q0qH2QBdXL8WORWADrpUb/P2KDvyk++lt8Ws29gAiF++iPzA4AJ8hSUVZSn0D7eyogxfQH8/vfAxFggEKLMtpg7ASBqPBgGSY3eI1nhQKYGsSXBlDrXEgMkEQUhhjnBgADB0uuuuuuuuuuuuuFmEgBsT4jj3wOquQ6q58sJQIQ155EV+xzwUJ666666666666666666666666666666666666666+EfpT2GwpcxECpm+iV9lPCoVrrrrrrrrrrrrrtYtG9Iv1vxxl+LR/Tr92y3bL9rFolv/s3Frt+zf0i2sWjekXyTLobntHrrrrrrrrrrrrrr/gw+1rDgU+NpurEvNjE4037KyUK111111111111111111111111111111111111111/pT4Uw4FAxJjTSOZnifv2P1i6hWuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuv8JYfkGhTgj32DGv1/6KTmBfVpY76zPVQrXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/SP00HAhCPfyUByCibd0An4nDHM///ARLU+e/1F1111111111111111111111111111111111118mH2BvkDYS2PBpT7a3KfQLr3U75TCddddddddddddddddddddddddddddddddddddddddekw+lPhsJXkXS2IMN946D2vE/E/UJ11111111111111111111111111111111111/2t9mKHAlDPeM40J3QJV8b6e80J6GYq6666666666666666666666666666666666666666+fp9NILgo+QLL/F1Ctdddddddddf/8eEKgowHABAHAHgDCZ8BwiEMTP/+4wr8ADmCOLUoIDKlxtx/mGde+i//wASsI42xZholBX/5/ggE/ihrI3BUHW0iHg6ghripq/wR7X8//8eqDocwAmQGWiGCcqLtDg6EWs/MdpmFlfgMK68ji9yIF1tNYGh3KaTwoWhk88FMN2/tf7+t+omuuuv//xw6CbgOAiFsTMsAAQOMBEIAAqDGH8STOEeNhwEAYz/8Q9gtLgOCOAO8maEFnfCLm3gBOmwSnCOL9XvUEdddddddddf7ezfjQpwR77M897gzoil+K6hWuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuF0MAAit3d13wEeaOvN//X4d+/T6aew2b5AtOIi8gWQG/xP1Cf8Q/8PgqAcABAg9hMLAAED7/qBp4pSIgAUxxslMAWyAUQwDff7GBsykZkE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Lo/3BgAIB/gOi2A1Fa2///8mC4LwR77TmCZf/G+yb5f4K8yUE29HoGYXhVQfUI1111111111111111111111111111111111111111111111885/nxwSkA3nAmdLolB8e6q7/5/YID4D1QITUbXXXXXXXfXXXXX/w/0CwPV0K0/gHPGjErVXv8Aw/6BUIA7sIABOOsQJlO+5ZAAzIBI1hqUWm+r31BPXfffXS3110tf//8EIrgDHz0mPgfIshb5BorgRqR717sWR7+gd70WeqqFa6666666666666666666666666666666666666666666///4IQ5wAXiGTx7xDi77B6vf/H1h0VkBBTGXwAH8UYNef7X65RKvzabf/uHsFpfBBEDuVI1J8qfB6ev6f/9Pgg4ACE/tbgmw4QquMuKQYWoPQ4sp3v6on5/RKQXDeABTaJMNejI///6QPGu8a7/6/YVFfvTJ5DyPlRA8ABAiLRptZUA/pUu8Dgf//pCvAASjF1VsvKQXkB4ADfzAhTNxwpVsJkFmYMLumqKJEJH7+pQQ111111///SCEEGAA38wIUzccKVbCZBZmDC7pqiiRCR+/q///BD4ACBEWjTayoB/Spd4HFBDX//9YIQQYACRkQ0UhRxHL5fY5IKDtXpeCW0BS6///BCJwAXkEUL+GONJdfeoZrrr///ggBBwAGivG81ZXAT8iZJAU//9YIeAAsiD8ES0xSUi1PMNiILbdfxoT4SwUENf//8EIIOAA38wIUzccKVbCZB/9OGCD2Zgwu6aookQkfv6gFDTEThlQNUKlxE67//8EPAA9AiByGSEqETImIp6gh///ghFcAitqdefIikRfgrFcEe+yB1hfH+P9QrXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXfxh/+g0CbAwAAiAAIAZCZyQAAQeaA/T/XXDQKpbDv34AgJRSHGtc/AAg3y205xBi6vDyYJdgFWrAUVi0pfm779MkMyI6jSY1u/8BmAWtGXObQhh9Lk+yffqCeyZGnTC8g9HfvGbiy5EgxL5rBDvdIRR7ijHFvW8zQ0dWPkHxfYClineJGGtxmKVkMGWEFlHLU6wGTACk0EKXjjgAUgiTTamAk8q1PnpeMz/9cJJNB+xh5+ABG2DRL3CH8RTpH6SNX9v9EMfhV1B9qYf46o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",
    ".mkv": "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",
    ".avi": "UklGRvA7AABBVkkgTElTVOwRAABoZHJsYXZpaDgAAABAQg8AqGEAAAAAAAAQCQAAAgAAAAAAAAABAAAAAAAQAIACAABoAQAAAAAAAAAAAAAAAAAAAAAAAExJU1SUEAAAc3RybHN0cmg4AAAAdmlkc0ZNUDQAAAAAAAAAAAAAAAABAAAAAQAAAAAAAAACAAAAvRsAAP////8AAAAAAAAAAIACaAFzdHJmKAAAACgAAACAAgAAaAEAAAEAGABGTVA0AIwKAAAAAAAAAAAAAAAAAAAAAABKVU5LGBAAAAQAAAAAAAAAMDBkYwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABKVU5LBAEAAG9kbWxkbWxo+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAATElTVBoAAABJTkZPSVNGVA4AAABMYXZmNjIuMTIuMTAxAEpVTkv4AwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABMSVNUpiUAAG1vdmkwMGRjvRsAAAAAAbABAAABtYkTAAABAAAAASAAxI2IAA0UBC0UQwAAAbJMYXZjNjIuMjguMTAxAAABswAQBwAAAbYWGRhWbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfgAAhQxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bf/I22/j7b+Ntv482/jbe+Nt7sbbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bb/xv98j7b5G238bbfxtt/G238bbfxvt/G238e3fY29vjbb7G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G234AAI8MRhWbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtuuR/t8jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+N5/483PY22+xtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfgAAlAxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtuuR02+Rtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/HLqextt9jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Nt/kfffI22/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+N03yOX92Ntvsbbfxtt/G238bbfxtt/G238bbfxtt/G238bbfvwAAngxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238brvkbbfI22/jbb+Ntv422/jbb8itsaJxH94qYD3TYFIDBLpMukRBPqZEdkGbhv8vTtjdulGFUCoziMSG9GhBtTlRWUZuzLeL07Q2bpRpXAr+pDWjUiNihMIfvlTIeaHIFKDhsFArx65cI4229422/jbb+Ntv422/jbb+Ny/2NvPsbbfxtt/G238bbfxtt/G238bbfxtt/G238bbfgAAowxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G2/yOm3yNtv422/jbb+Ntv422/IrbGYjqyzwgYN/oyTwR6ZimxxCDAPR7W7WmqBhDQIg4RApGYzdBfzJk46NtvSNtvyGxYPB7WrGm6BhBQIA4RAoYz33Js48iBfY0EZWW4IGo/DYFcDhECgVwGDcUWujbb3jbb+Ntv422/jbb+Ntv45f3Y22+xtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G+38bbfxtt/G238bbfxtt/G235BMbF/xyrb0lUQow56tLSR5iiCSDeiYnxZUkbTh0iiPaRYSiNwDSyObqkpBMpre32dcSpKx0yxilbcQqEAmPE/8ntUolF6NA3bsfv5ekBOyKodMs4pWzEClCJmF73L7ygbKJwaez++k4QkqqtI2mG6KothEZOCPwES9R3cUFIJgYtGtv868gxqXfHCtrSXlKNOBWMfF8lNyBRBBLBtRNG3nAyjbb+Ntv422/jbb+Ntv4/L/xt5/G238bbfxtt/G238bbfxtt/G238bbfxtt+wAArQxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/H+3yNtvkbbfxtt/G238bbfxtt+S18OtDTQoDe/h1oaaFGYKkgiiaHk2m/sXvt4GVKXNRguISbaVWzOf1Y1FnGG/kkRHCGy7SVWwt7VjVXczH2SozsK22fs3v96GUKHKt+T8KPg68GmBRn4OvBpgUKGKURBPHDbUNY22/jbb+Ntv422/jbb+N5v4829o22/jbb+Ntv422/jbb+Ntv422/jbb+Ntv3wAAsgxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/H/3rG238bbfxtt/G238bbfxtt+y18OtDTQoDa/h1oaaFDd4VFwiiaNvNwsjbb+Ntv422/Z2FPwdeDTAo78HXg0wKEcGZeIgnjhtoGsbbfxtt/G238bbfxtt/G238ctvaNtv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/ZX8OtDTQoz+NtDTQo1flwiiaNtuyNtv422/jbb9n28HXg0wKG/B1gaYFFdC8RBPG238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfgAAvAxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/H+3rG238bbfxtt/G238bbfxtt+wfq8sNx4a8Jko8U2/aR1HhslcY8QTfmWyoSEpbGQGlYMa+NCrAlf29bDzDQi0JDAqv8i6lzVNy5MtArOwYnqFqtNjPpg2UB1TQRDnS5X4ClpLh2HjvM5pI9rczLs2wCk7RieeFyv4FLCXTgmVJsY9cG6kO6bJBRnv7OYSvKaYyZEr+XrQe4aEWBKaA0rBjXxpSZ/KupcwTCpi2SGpAoFqQeKLPso4iwMX4twap/zeSICGPNssfjbb+Ntv422/jbb+Ntv423vjlt7Rtt/G238bbfxtt/G238bbfxtt/G238bbfvwAAwQxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/H/q5G23yNtv422/jbb+Ntv422/jbb+NtvsbbfI22/jbb7G23yNtv422/jbb+Ntv422/jbb+N5vsebfY22/jbb+Ntv422/jbb+Ntv422/jbb+Ntv3wAAxgxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G6e5G33yNtv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jc59jbb7G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bzfY6e+tLY2ANB4OAdbDVoGCkM4jN1MP036NmyoCW0sFxntLYQ7ZktqKS2lDgYyv+q2209Aq3RevIMjH0thDEoHg4BcuBgV3gopWJI+TVQ2zFIcZ3bClQiE/KW5TDsuSrMMzaHC+aMLwKi27npd2zMi9tWjvrZ3wkj5tkPsb6324oUfR53q6jhAfwD4lb8sitfvO3NmAj2HgyjoHg/9eAwK9OD5n/z24Vj1IksitmeuoovVja0vISvKo14iq7hQEFoSPs+pen+q+WCJl1T7tDa8X6Egk8WY15QBaezloEJi1vRVyls9oHxK9eB/KCuzdF8h4smH6ZKDDZuf4jiLYU5Tvlb6nA5gxcfEdnZcYl5FCneISQoOXAhbclHM7Z3nFhiEUFZ6cl3VHURwpzFqRrWGON3tXtX7yEgm2F6rZtn5zF972BTT+I+LyIXERusHqb9UT65LLApK9ssRw8UtmtH6b15frknaFCTTLTIdbKGY05yo+U8Ws2raY4Od51EtaFCWWbJsRVdwzCCjYKNCYWpgYOmAYowKIVcDzNQvKc/heq/qj2qaGmhIfHZfZc8q+vOltwKK09tvEQpKWyLReqbszP93hqmlyBL7fmp3+zAX2ZyouU+RG14PUm3g5GMsCsp9liOHiltfyVj6lR8bL6jnIJ+0lYZ1R7e2Lon85TVWI42y+3jzb7G238bbfxtt/G238bbfxtt/G238bbfxtt/G234AANAMRhWbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G6e5Y2ANB4OAdbDVoGCkM2tdCEmTxG0nZ9SjeXBhsCtgGg8HAOthq0DBSJxKYA2AYy0husqA1zm4L3fYhHo96BzG4N0uY0MwYRQco0aj1KwIAkDo2zrLNGc8oB2HgysDlLUg8EQQbwkHPC0YSUKxGDWSYPpEUHMhRILPsLUZqoA7G4N8LsUFNnIFIaR5R0W/0SxzocSdqm0b3bL0JdjAGwOeYiP5dnA152C9yQlK1Rc234dppsqxW1N1rcJNQVCe++n+kaS4yJF/MnNK+xR+VeTq9ygPcJAUaujhI1Fo31vRlxRArFAMpA5d97w+z9/uYDB17JFN4hCRgpVVrSpnIs132kk3iilHZwZu+4HQHC5sfe+zl37OKVKmxR1Eo4siNnxsJHhwB/3kZY2znQ0z7lB0AamYLvNMz5axFBVnS2QbLoDR3CRt+B/27ObrbO2Grc/T32XBqDAXaBgVwOAqZCv5RC7xbyeD7FKlFwsnT7BqDAXaBgVwOAqZHAH1IMCIwoRAxUWhoWISRSe+wyg8FActLUQWQ3AyLuFjjokbMCG0IFo41vcnUdavedFwnUEYeiUkVaH7XWVNvRxPqYgktG3AjZdboH2NvhwWpy0sRcn+7guf5YUx+XAbkuB3htFYaCQZYnSj6ln5eBx8HFWg6u6seh8yWb7sUS9lXXFSRePWGWCzMvVl9kKgXihb4t5MvlPqFRYmxnFbF/gcasavCIfDxiK2VGjbimw2eODr1xJ+0CqmwoUc6J+XMh82po25y8FeuTgfzwmLQEtJ5gs3y03ssJXFdV6DDjN6V7z3RnZaMHUPUuJyzPd5hIjtFU6m0GLMaArkn+EqOwK/ERgtRqUJoFwWMEwKqSgbU2oJO84MiH+3PF+VVaWKZF0fNRx8FzKr0uK2pV1tLJtKKQkfKx59v3vMTtqMcZOIRTCttl0q/Zw2AgsmEFGwUaExhtLdaaTeRKPTabpoohCsEFGwUaEyWjyUu837paoLdDeVBCggr4oLGlJu8FJYoSJImw3+jfQwgqHI7axpnnpyXFIzteZEpKWwQPh3g2oc4A+BQiIcBW5UF8GVfv5TGbNQ9GZMWJmC5V65/M5vDdQuHHwclHqi1YtLCjMJrYHrDd96KVNhpFyE+ZlLh+W8WLS2EsqOCnvlKne9UqCo+WUH48Y8xf+ii5xDb2IiFIRv3WLlApViihJReO2m2Cz3SvneznIR4SGr5i7Vi29GURDXt6IAeo1K3SMmGypqssjiXinqkoh0Qqi5pNVOKe2cXBMLiUDgU4yFpX1e3qhAejKzmwYxtt9jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb98AANUMRhWbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238izbXmcLG8n8oiVEcbTDSrCxj09ne8iLr0a5It4eFzXxz5V7PrlcRrkcMpFSfgftY2oRKUHSMq+SJZmeyZk5mTkkQOXZLkicGRse/TdhWMYdZb5LaEofMVT5M1NN87YUKT5SyWcs7KjlRPb+Q5gkDz2luJGpqOLIiiU4kCmTCCBsQMD9GORF7zgUEn8nZHyRPVKtjzdk5LpomQUYoLORRxQoQcRcFDfyHIIw6zVMSNdKOi6rBUhUiasxXWMrM5xsRZwiIvySPtNNh37NKYE6kLk2LxjAzClvXtN0alCOjbQ0oUD4RpNZ2WdlavZSkwxM0kYYqn2cvOI7Vhm+E5vM/A5IiEJsSKvVTjWSmwTUEHT0ZGSLfTKvr61lptCFT8qYaDr2Sm0I1RfO03RrJP2Gm+FvpvER+JJkki0iJzfSRYDSqapjWKe96K0/+83OfybBWg/7TbyS/JlTV75qSmyFPcz85uTYK2/7SchLQuTZexjOjEKS5d/20jhLYqY9VONZmm3Ioz2OW3tG238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G239qh+xVo34Edycs1eLwBy1Cu8BgLsgrlQPmf/Iw8pxrPz+gwdqFCyhRqu5VuDM/ypyyojfBnCOY63svNqytmWTZn7kRKLQ4NLClK1Sktl1djVxFREP3fZ2RDUQmfZOLo7yEr1VwZbJ/eaw3UfQKZwYEF+AsPF7e/aR3vA8qzvpzbWq9uaCps2X+KFHbOT3FM2FJ9JX/WrA39uSAxKsWDE79Lasb2Lf2gxVjYg1Dq/GhcAnZran/lNnVSmDnc/ze8lKosKPpbRYo3ylqmKOzdmow42IKSjRet0tzcuqKrHC+5yiJeNDcZkf1v2yc6hvIKcp/PK2y2lX28yCJIp52xCoI5bwGRSN7eq8K7ZbCrUSxD9TtkXpze7uRXFNpZ9hRaj59R2H8p1SH3apEHvUIfB2D50AT9b9siNZeQZ0iprWduaCps2X+KFHbOT3FM2Cm+3fNfrd7U+B4tycbG0iPuHtqcgXVzv0xXuB7UV4BQze7QYro5vhAG0W4HSlTocqasEdGKW0290PlMKtAwMu3D6ny2fxqX9Kt3pbmbb3iOxFA4hsm+t/svYhiMk6dcv7fe/8sAxdt9YV6p4jJIveE9KVGqxv3fsZFFnba1ENWhSNPqdsiM3wZ0i3n/8s7SpKx5HNqjVHZdhG7W1G/HO9icskWUKBzMRcXRnPrft5O1FF3l/djbflN2SesUoytT0KDDduwv63cHE5MW5ws72m1NvAo+32VFUEoyqIavsnIbREpDPf1TmbzjSTiBH1Tyr8uyEam/oGFE//+NtxR+y3ogVbsWsQmyD6cyTS/pVKns6poDkHDSAiN3f+82p/ogiBdlU5vrywrzkKiihX9MtO/HGgX3+VVreorznOXDSnqIisct5WxveJq3Ju5+96GyINBp2luP3fNS43v7+tS27i01Di3M6iNOSauxSOasphYp/m6ugtnEI2IeRtl32Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv3wAA3wxGFZt/G238bbfxtt/G238bbfxtt/G238bbfxtt/S2Z1m4H8W74sHBKOVs6DvnHjN0PkXfqS0kLFt4DvnOX+TvUJsUdRiLk53vOIe96MTs1XIpKv9mX8lyxTMR8Cdh/FGW5/v0XlBXVu3kDgV/V616o/ZV+ryI6jonig/43zm8ECfmT3d6pveiZO/BWa34CF3Bp91PeiLMiNFIhRjXlnoizIupRQbmxTXmZqiFfbimdyyai6Fniucwb/txSVeQd53hB98sxSpUYpDnk6NdZKjneXsJKT6K4Wt5c2T3O7YVIlK5oVOM5Q5iymKcsU5EOGwp+2oi60K0ayMpJ6LPRF7IgRO8V+qgb8/ZM1QtbZ1GMhqnNluyCJeUp4f5emZg2yYUQ9yz3eqM6b5D9a3Mswru2dkydWhHcV+1RxSVrZqiqLQ46T/c8hpGGJHU96IvZF+rSIzYpkQc7xdeqJqiqLsQTSevKy3LpZ+ctg2R824sSmyb7yjF1ovzqyPiLoo3J1Tyd5zqHho/hB9y1ecqi5cqlTUUUG705WM9+2teezyyilvap6shQHvuiz3VKjFIc8nRqyz3elmdN8h96nkzdUb3LJcs5StEJorck7cbNh1VpA5Jvubk1bZLCWH/fZeW5lsRdkOJI1ydTW55HN4sVqKonTb/vWSozvuZo3k0ph+tV+y7CsPBvzLVE53tFT1nJ1HaoUlWWyRBdFP3771nLmWRFOTjsWYvFGLo1ovxENJa1S17ajvpaNKD/CvqnFGKapywqNu+2WYHEQVEhiIaNRhqI+okKPpSfqq+Fgefpbtg2DbvYDnyH/uSrr/HCIRUS4S/e4hQk/vvVe5lpux2rOTqPs0s5J23ed5CiIybh/7nVxuWc5VKkOLEZ/7ZZlR5i/EBpEMCBzMnOZJxEsiFFCDz9UlWCBN5YV9u3OcN1A4t1bvKUcXGBP24kkRQHdI1tVYWfz0zxbOdkCRYP/ctqnOXMsuXsUjZYV4P8iD/eKeFlUzDexA77TnvTvMydR8kXFLnvYNsyFEjndhZfyrKFM1Gg73hCoH+RYr2zM2qPWrUh+3PeneeyKUZVFxSyz0nJ7JO95yIzYpYfytz0zbL6fUyLLohkK5jcUqObLe8U5ZN4j4/6YZnU+Fm/mf2ZLeqKo3hqhOhlUzvLCTVn/G3L7G238bbfxtt/G238bbfxtt/G238bbfxtt+8AAOQMRhWbfxtt/G238bbfxtt/G238bbfxtt/G238bbfyGPsKWWvoIHcNqF/DAzUVbPq9U8UyjgFRdX2gjnS75IpEm9XUQFZ9q83/7bshWVhQmWMgY0PFPsvudqjcKl+qFqVGnt/InM0clt4BaYsbU2jSo1YmEBZSWbhIv0JW/kksqlVQY1myjPJFhouH3xx7OeU5nByp3NyknIvCQ4yXyRWNogzsUM+kuiLLMUIkV6jRuxY3tl+WB5myKOld2KO3snISHW/kr8nkkzbJfezlBU+5zhtd2an8LM4N5rbdNft7wXjBzfyJqAVs+gntwlX2UJlYxsxiLB42W0oK0Q1f+S/Jiywq9ZN7PqdUm4VDbsNn8IDagsKuDm/G+wty7dubyFSNB0+/yTxNsl4olEHGchXcKpxRbVxmFWrTOjm9yX44Wl1F63uXeSlZtzfyUBreMjbC0s5Jdsl8btXXBO32G272li/ucLf89V15yQt6sj4RN/Ii0pgeWLez06HNUFYycu2q/+/G1U8UW7oeIiqYNhmSkTJfIiOKDFaIFbFgYYKDtjhvOqCwPfey96VXMUdiy8JDjfyJlmh8OJeaOFCk1ecPmQRND0OsUsFg3/q/Ze2mWL/kLN5tHFznfb3lWlKriNc2TK1NbrfbFG5VENN7Mm0HZOCd7smjH5GtKq3l/uo1Gjkbr87EHFiCRA1bbxSH8Z01JMUchtATN/I21V+2W95Z6DimvzbV4hnFhqm3n/tci2lXV7yhzimL8qI+38i8TauNp36reS7eDirr1G+G1Sn5bzP9yZZu7iLq8udNrw8SfyJXwKytTkDy5DUW4NKxqy/qxW014Z5NlClv5Lxj/hxxQsqjGo1uCLiy5RT1+TbN9dUFSmbqn+79TnOlaIoCtv5JHDYgVVSzhZfZ9fobdzgxOoQcrKSyTi/Laug4tFuEbfxptvsbbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt9jbb+Ntv422/jbb+Ntv43m/jbb+Ntvkfefxtt/G838bbfY22/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb+Ntv422/jbb98AAO4MRhWbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfxtt/G238bbfvwAwMGRj0wkAAAAAAbZrhhDGFZt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtv3AACFDAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/vFvDYYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/hjCw29Qxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/hjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234AAI8MBjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/hjCv+9Qxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxvP/DGFZt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+AACUDAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/DGFht6hjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb+GMKzb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb+GMK/71DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DGFht6wxhWbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8MbbfgAAngwGMKzb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb+GMLDb1DG234Y22/DG234Y22/DG23/hjCw29Qxtt+GNtvwxtt+GNtvwxtt/DGFZt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+/AACjDAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/DGFht6hjbb8Mbbfhjbb8Mbbfhjbb/8MYWG3qGNtvwxtt+GNtvwxtt+GNtv4YwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/DG234Y22/DG234Y22/DG234Y22//DG234Y22/DG234Y22/DG234Y22/hjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8MbbfsAAK0MBjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG238MYWG3qGNtvwxtt+GNtvwxtt+GNtv/wxhYbeoY22/DG234Y22/DG234Y22/hjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG237AACyDAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GP/vUMbbfhjbb8Mbbfhjbb8Mbbfhjbb+9vb78MYWG3qGNtvwxtt+GNtvwxtt+GNtvwxtt+GOW3sGNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/eXhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb9/AAC8DAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GP9vUMbbfhjbb8Mbbfhjbb8Mbbfhjbb/8MYWG3qGNtvwxtt+GNtvwxtt+GNtvwxtveGOW3sGNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtv3AADBDAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/DGFht6hjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb6GNtvgY22/DG230MbbfAxtt+GNtvwxtt+GNtvwxtt+GNtv4YwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+8AAMYMBjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG238MYWG3qGNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtv4YwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt/DG3v///hjCw3tYY22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG237AADQDAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtv///wxhWbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8MbbfsAANUMBjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG23///hjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22////DGFZt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+wAA3wwGMKzb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbf///4YwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+/AADkDAYwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt////hjCs2/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234Y22/DG234YwrNvwxtt+GNtvwxtt+GNtvwxtt+GNtvwxtt+8PwxhWbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb8Mbbfhjbb9wAA7gx//////78AaWR4MSAAAAAwMGRjEAAAAAQAAAC9GwAAMDBkYwAAAADKGwAA0wkAAA==",
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
            row = conn.execute(
                """SELECT COUNT(*) as total_items,
                          COALESCE(SUM(original_size_bytes), 0) as total_original,
                          COALESCE(SUM(dummy_size_bytes), 0) as total_dummy,
                          COALESCE(SUM(original_size_bytes - dummy_size_bytes), 0) as total_saved
                   FROM archived_items WHERE status='archived'"""
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
    def trigger_library_scan(self) -> None: ...

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

    def trigger_library_scan(self) -> None:
        try:
            for lib_name in self.config.get("libraries", []):
                section = self._server.library.section(lib_name)
                section.update()
            logger.info("Plex: library scan triggered")
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

    def _request(self, method: str, path: str, json_data: Any = None, data: Any = None, params: dict | None = None) -> Any:
        self._ensure_auth()
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = self.headers.copy()
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
            with open(source_path, "rb") as f:
                self._request(
                    "POST",
                    f"Items/{item_id}/Images/Primary",
                    data=f.read(),
                    params={"X-Emby-Client": "MediaSpektor"},
                )
            return True
        except Exception as exc:
            logger.error("Jellyfin: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self) -> None:
        try:
            self._request("POST", "Library/Refresh")
            logger.info("Jellyfin: library scan triggered")
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
            with open(source_path, "rb") as f:
                resp = requests.post(
                    url,
                    headers=self.headers,
                    data=f.read(),
                    params={"X-Emby-Client": "MediaSpektor"},
                    timeout=30,
                )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Emby: upload poster failed for %s: %s", item_id, exc)
            return False

    def trigger_library_scan(self) -> None:
        try:
            url = urljoin(self.base_url + "/", "Library/Refresh")
            requests.post(url, headers=self.headers, timeout=30)
            logger.info("Emby: library scan triggered")
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

    def unmonitor_movie_by_path(self, file_path: str) -> bool:
        try:
            url = urljoin(self.base_url + "/", "api/v3/movie")
            resp = requests.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            movies = resp.json()
            norm_path = os.path.normpath(file_path).lower()
            for movie in movies:
                movie_path = (
                    os.path.normpath(movie.get("path", "")).lower()
                )
                folder = (
                    os.path.normpath(movie.get("folderName", "")).lower()
                )
                if movie_path and (
                    norm_path.startswith(movie_path)
                    or norm_path.startswith(folder)
                ):
                    movie["monitored"] = False
                    put_url = urljoin(
                        self.base_url + "/", f"api/v3/movie/{movie['id']}"
                    )
                    put_resp = requests.put(
                        put_url,
                        headers=self.headers,
                        json=movie,
                        timeout=30,
                    )
                    put_resp.raise_for_status()
                    logger.info(
                        "Radarr: unmonitored movie id=%s path=%s",
                        movie["id"],
                        file_path,
                    )
                    return True
            logger.warning(
                "Radarr: no matching movie found for path '%s'", file_path
            )
            return False
        except Exception as exc:
            logger.error("Radarr: error unmonitoring movie: %s", exc)
            return False


class SonarrClient:
    def __init__(self, config: dict) -> None:
        self.base_url = config["url"].rstrip("/")
        self.api_key = config["api_key"]
        self.headers: dict[str, str] = {"X-Api-Key": self.api_key}

    def unmonitor_episode_by_path(self, file_path: str) -> bool:
        try:
            # Get all series
            url = urljoin(self.base_url + "/", "api/v3/series")
            resp = requests.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            series_list = resp.json()

            norm_path = os.path.normpath(file_path).lower()
            for series in series_list:
                series_path = (
                    os.path.normpath(series.get("path", "")).lower()
                )
                if series_path and norm_path.startswith(series_path):
                    series_id = series["id"]
                    
                    # Get episode files for this series to find matching file ID
                    file_url = urljoin(
                        self.base_url + "/",
                        f"api/v3/episodefile?seriesId={series_id}",
                    )
                    file_resp = requests.get(
                        file_url, headers=self.headers, timeout=30
                    )
                    file_resp.raise_for_status()
                    episode_files = file_resp.json()
                    
                    episode_file_id = None
                    for ep_file in episode_files:
                        ep_file_path = os.path.normpath(ep_file.get("path", "")).lower()
                        if ep_file_path == norm_path:
                            episode_file_id = ep_file.get("id")
                            break
                    
                    if not episode_file_id:
                        continue
                    
                    # Get episodes for this series
                    ep_url = urljoin(
                        self.base_url + "/",
                        f"api/v3/episode?seriesId={series_id}",
                    )
                    ep_resp = requests.get(
                        ep_url, headers=self.headers, timeout=30
                    )
                    ep_resp.raise_for_status()
                    episodes = ep_resp.json()
                    
                    found_any = False
                    for ep in episodes:
                        if ep.get("episodeFileId") == episode_file_id:
                            ep["monitored"] = False
                            put_url = urljoin(
                                self.base_url + "/",
                                f"api/v3/episode/{ep['id']}",
                            )
                            put_resp = requests.put(
                                put_url,
                                headers=self.headers,
                                json=ep,
                                timeout=30,
                            )
                            put_resp.raise_for_status()
                            logger.info(
                                "Sonarr: unmonitored episode id=%s path=%s",
                                ep["id"],
                                file_path,
                            )
                            found_any = True
                    
                    if found_any:
                        return True
            logger.warning(
                "Sonarr: no matching episode found for path '%s'", file_path
            )
            return False
        except Exception as exc:
            logger.error("Sonarr: error unmonitoring episode: %s", exc)
            return False


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
        self.font_name = aest.get("font_name", "Arial")
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

            # Draw 1px border at top of banner
            for x in range(width):
                img.putpixel((x, y_start), self.border_color)

            # Text
            text = f"ARCHIVED \u2022 {gb_saved:.1f} GB SAVED"
            font_size = int(height * self.font_size_ratio)
            try:
                font = ImageFont.truetype(self.font_name, font_size)
            except (OSError, IOError):
                logger.debug(
                    "Font '%s' not found, using default", self.font_name
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

            img.save(output_path, "PNG")
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
                            / f"{server.server_type}_{item_id}_poster_overlay.png"
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
                        self.radarr.unmonitor_movie_by_path(file_path)
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
                server.trigger_library_scan()
            except Exception as exc:
                logger.warning("Library scan failed for %s: %s", srv_type, exc)

            self.db.update_status(srv_type, srv_item_id, "restored")

        return True

    def stats(self) -> dict[str, Any]:
        return self.db.get_stats()

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

                        poster_overlay = self.backup_dir / f"{local_type}_{local_id}_poster_overlay.png"
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
                    server.trigger_library_scan()
                except Exception as exc:
                    logger.warning("Library scan failed for %s: %s", server.server_type, exc)

            if media_type == "movie" and self.radarr:
                self.radarr.unmonitor_movie_by_path(file_path)
            elif media_type == "episode" and self.sonarr:
                self.sonarr.unmonitor_episode_by_path(file_path)

            results["success"] = True
            logger.info("Successfully archived and 'Spektored' item: %s", title)
            return results

        except Exception as exc:
            logger.error("Failed to archive item %s: %s", item_id, exc)
            results["error"] = str(exc)
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
logging.getLogger("mediaspektor").addHandler(memory_log_handler)
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
            secure=request.url.scheme == "https",  # mark Secure when served over HTTPS
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
def update_config(req: UpdateConfigReq):
    global GLOBAL_SPEKTOR
    try:
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
        logger.info("Configuration updated and reloaded successfully.")
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class ChangePasswordReq(BaseModel):
    password: str
    username: str | None = None


@app.post("/api/change-password", dependencies=[Depends(verify_auth)])
def change_password(req: ChangePasswordReq):
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
        logger.info("Dashboard password updated.")
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
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            return StreamingResponse(
                resp.iter_content(chunk_size=1024),
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"}
            )
        elif server_type in ("jellyfin", "emby"):
            url = urljoin(server.base_url + "/", f"Items/{item_id}/Images/Primary")
            resp = requests.get(url, headers=server.headers, timeout=30, stream=True)
            resp.raise_for_status()
            return StreamingResponse(
                resp.iter_content(chunk_size=1024),
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Poster proxy failed: {exc}")

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

@app.post("/api/restore", dependencies=[Depends(verify_auth)])
def trigger_restore(req: ActionReq, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(run_bg_restore, req.server_type, str(req.item_id))
    return {"success": True, "message": "Restoration process queued as background task."}


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
        uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    main()
