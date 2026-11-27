"""Small media files built in the test, one per allowed format, plus the pieces to make broken,
disguised and polyglot ones. The checksums (Ogg CRC-32, FLAC CRC-8 and CRC-16) are computed here
bit by bit, independently of the dispatcher's table and zlib based versions."""

from __future__ import annotations

import struct
import zlib

GPS = b"GPS 47.3769 N 8.5417 E"
"""A marker standing for location metadata: it must not survive the dispatcher's cleaning."""


# --- checksums -------------------------------------------------------------------------------


def crc_bitwise(data: bytes, poly: int, width: int) -> int:
    """A non-reflected CRC with initial value 0 and no final xor, one bit at a time."""
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    crc = 0
    for byte in data:
        crc ^= byte << (width - 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
    return crc


# --- images ----------------------------------------------------------------------------------


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(*extra: bytes) -> bytes:
    """A 1x1 RGB PNG; `extra` chunks go between IHDR and IDAT."""
    header = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + header + b"".join(extra) + idat + png_chunk(b"IEND", b"")


def jpeg_segment(marker: int, data: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(data) + 2) + data


def jpeg(*extra: bytes, scan: bytes = b"\x12\x34\xff\x00\x56\xff\xd0\x78") -> bytes:
    """A baseline JPEG skeleton: APP0, `extra` segments, DQT, SOF0, DHT, one scan, EOI. The scan
    bytes include a stuffed 0xFF00 and a restart marker."""
    app0 = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    dqt = jpeg_segment(0xDB, b"\x00" + bytes(range(1, 65)))
    sof = jpeg_segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
    dht = jpeg_segment(0xC4, b"\x00" + b"\x01" + bytes(15) + b"\x00")
    sos = jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    return b"\xff\xd8" + app0 + b"".join(extra) + dqt + sof + dht + sos + scan + b"\xff\xd9"


def exif_segment() -> bytes:
    return jpeg_segment(0xE1, b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08" + GPS)


def gif(*extensions: bytes) -> bytes:
    """A 1x1 GIF89a with a global color table; `extensions` go before the image."""
    screen = b"GIF89a" + struct.pack("<HH", 1, 1) + b"\x80\x00\x00" + b"\x00\x00\x00\xff\xff\xff"
    image = b"\x2c" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00" + b"\x02\x02\x44\x01\x00"
    return screen + b"".join(extensions) + image + b"\x3b"


def gif_comment(text: bytes) -> bytes:
    return b"\x21\xfe" + bytes([len(text)]) + text + b"\x00"


def riff_chunk(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")


def riff(form: bytes, *chunks: bytes) -> bytes:
    body = form + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp(*extra: bytes, flags: int = 0) -> bytes:
    """A VP8X WebP with a VP8L image chunk; `extra` chunks after it."""
    vp8x = riff_chunk(b"VP8X", bytes([flags, 0, 0, 0]) + b"\x00\x00\x00\x00\x00\x00")
    image = riff_chunk(b"VP8L", b"\x2f\x00\x00\x00\x00\x07\x10\x11\x88\x88\xfe\x07\x00")
    return riff(b"WEBP", vp8x, image, *extra)


# --- audio -----------------------------------------------------------------------------------


def wav(*extra: bytes) -> bytes:
    fmt = riff_chunk(b"fmt ", struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16))
    return riff(b"WAVE", fmt, *extra, riff_chunk(b"data", bytes(160)))


def mp3_frame() -> bytes:
    """One MPEG-1 Layer III frame, 128 kbit/s at 44.1 kHz without padding: 417 bytes."""
    return b"\xff\xfb\x90\x64" + bytes(413)


def mp3(frames: int = 3, *, id3: bytes = b"", id3v1: bool = False) -> bytes:
    tag = b""
    if id3:
        size = len(id3)
        safe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
        tag = b"ID3\x04\x00\x00" + safe + id3
    tail = (b"TAG" + GPS).ljust(128, b"\x00") if id3v1 else b""
    return tag + mp3_frame() * frames + tail


def ogg_page(body: bytes, *, sequence: int, flags: int) -> bytes:
    segments = []
    rest = len(body)
    while rest >= 255:
        segments.append(255)
        rest -= 255
    segments.append(rest)
    header = (
        b"OggS\x00"
        + bytes([flags])
        + struct.pack("<qII", 0, 0x5B, sequence)
        + b"\x00\x00\x00\x00"
        + bytes([len(segments)])
        + bytes(segments)
    )
    page = header + body
    crc = crc_bitwise(page, 0x04C11DB7, 32)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def ogg(codec: bytes = b"OpusHead\x01\x01\x38\x01\x80\xbb\x00\x00\x00\x00\x00") -> bytes:
    """An Ogg stream of three pages: the codec header, tags, and an end-of-stream audio page."""
    tags = b"OpusTags\x08\x00\x00\x00encoder!\x00\x00\x00\x00"
    return (
        ogg_page(codec, sequence=0, flags=0x02)
        + ogg_page(tags, sequence=1, flags=0)
        + ogg_page(b"\xfc\xff\xfe", sequence=2, flags=0x04)
    )


def flac_frame() -> bytes:
    """One FLAC frame: block size 192, 44.1 kHz, mono, 16-bit, a CONSTANT subframe."""
    header = b"\xff\xf8" + bytes([0x19, 0x08]) + b"\x00"
    header += bytes([crc_bitwise(header, 0x07, 8)])
    body = header + b"\x00" + b"\x00\x00"
    return body + struct.pack(">H", crc_bitwise(body, 0x8005, 16))


def flac_block(kind: int, data: bytes, *, last: bool = False) -> bytes:
    return bytes([kind | (0x80 if last else 0)]) + len(data).to_bytes(3, "big") + data


def flac(*extra: bytes, frames: int = 2) -> bytes:
    """STREAMINFO, `extra` metadata blocks (the last one flagged), then frames."""
    info = struct.pack(">HH", 192, 192) + bytes(6) + bytes([0x0A, 0xC4, 0x40, 0xF0]) + bytes(4) + bytes(16)
    blocks = [flac_block(0, info, last=not extra)]
    for index, block in enumerate(extra):
        last = index == len(extra) - 1
        blocks.append(bytes([block[0] | (0x80 if last else 0)]) + block[1:])
    return b"fLaC" + b"".join(blocks) + flac_frame() * frames


# --- ISO BMFF and WebM -----------------------------------------------------------------------


def box(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data) + 8) + kind + data


def mp4(brand: bytes = b"isom", *, mdat: bytes = b"\x00" * 64, udta: bool = True) -> bytes:
    """ftyp, moov (mvhd, a trak with a udta, a udta with a location), mdat."""
    ftyp = box(b"ftyp", brand + b"\x00\x00\x02\x00" + brand + b"mp41")
    location = box(b"udta", box(b"\xa9xyz", b"\x00\x12\x15\xc7" + GPS)) if udta else b""
    trak = box(b"trak", box(b"tkhd", bytes(84)) + (box(b"udta", box(b"name", GPS)) if udta else b""))
    moov = box(b"moov", box(b"mvhd", bytes(100)) + trak + location)
    return ftyp + moov + box(b"mdat", mdat)


def ebml(element: bytes, data: bytes, *, unknown: bool = False) -> bytes:
    if unknown:
        return element + b"\x01\xff\xff\xff\xff\xff\xff\xff" + data
    return element + b"\x01" + len(data).to_bytes(7, "big") + data


def webm(doctype: bytes = b"webm", *, live: bool = False, extra: bytes = b"") -> bytes:
    header = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x86", b"\x01") + ebml(b"\x42\x82", doctype))
    info = ebml(b"\x15\x49\xa9\x66", ebml(b"\x2a\xd7\xb1", b"\x0f\x42\x40"))
    tracks = ebml(b"\x16\x54\xae\x6b", ebml(b"\xae", ebml(b"\xd7", b"\x01") + ebml(b"\x86", b"V_VP8")))
    cluster = ebml(
        b"\x1f\x43\xb6\x75", ebml(b"\xe7", b"\x00") + ebml(b"\xa3", b"\x81\x00\x00\x80" + bytes(16)), unknown=live
    )
    return header + ebml(b"\x18\x53\x80\x67", info + tracks + extra + cluster, unknown=live)


VALID: dict[str, tuple[str, str, bytes]] = {
    "shot.png": ("image", "image/png", png()),
    "photo.jpg": ("image", "image/jpeg", jpeg()),
    "photo.jpeg": ("image", "image/jpeg", jpeg()),
    "anim.gif": ("image", "image/gif", gif()),
    "pic.webp": ("image", "image/webp", webp()),
    "take.mp3": ("audio", "audio/mpeg", mp3()),
    "take.wav": ("audio", "audio/wav", wav()),
    "voice.opus": ("audio", "audio/ogg", ogg()),
    "voice.ogg": ("audio", "audio/ogg", ogg(b"\x01vorbis" + bytes(22))),
    "take.flac": ("audio", "audio/flac", flac()),
    "song.m4a": ("audio", "audio/mp4", mp4(b"M4A ")),
    "clip.mp4": ("video", "video/mp4", mp4()),
    "clip.mov": ("video", "video/quicktime", mp4(b"qt  ")),
    "clip.webm": ("video", "video/webm", webm()),
    "live.webm": ("video", "video/webm", webm(live=True)),
}
"""File name: (kind, MIME type, bytes) of every allowed format."""

SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body><script>fetch('/x')</script></body></html>"
ZIP = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + bytes(20) + b"PK\x05\x06" + bytes(18)

NOT_MEDIA: dict[str, bytes] = {
    "drawing.svg": SVG,
    "drawing.png": SVG,
    "page.html": HTML,
    "page.jpg": HTML,
    "doc.pdf": b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n",
    "doc.png": b"%PDF-1.7\n%%EOF\n",
    "notes.txt": b"hello\n",
    "notes.mp3": b"hello there, not audio\n",
    "bundle.zip": ZIP,
    "bundle.mp4": ZIP,
    "tool.elf": b"\x7fELF\x02\x01\x01" + bytes(57),
    "tool.webm": b"\x7fELF\x02\x01\x01" + bytes(57),
    "movie.mkv": webm(b"matroska"),
    "movie.webm": webm(b"matroska"),
    "video.ogg": ogg(b"\x80theora" + bytes(34)),
    "film.mp4": mp4(b"3gp4"),
    "empty.png": b"",
}
"""Not media, or media of a type that is not allowed, whatever the name says."""
