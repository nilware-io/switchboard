"""Small media files built in the test, one per allowed format, plus the pieces to make broken,
disguised and polyglot ones. The checksums (Ogg CRC-32, FLAC CRC-8 and CRC-16) are computed here
bit by bit, independently of the dispatcher's table and zlib based versions."""

from __future__ import annotations

import io
import struct
import zipfile
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


def icc_profile(size: int = 128) -> bytes:
    """A minimal ICC profile: its header holds its own size and the `acsp` signature."""
    return struct.pack(">I", size) + bytes(32) + b"acsp" + bytes(size - 40)


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


WEBP_IMAGE = riff_chunk(b"VP8L", b"\x2f\x00\x00\x00\x00\x07\x10\x11\x88\x88\xfe\x07\x00")


def webp(*extra: bytes) -> bytes:
    """A simple lossless WebP (one VP8L chunk); `extra` chunks after it."""
    return riff(b"WEBP", WEBP_IMAGE, *extra)


def webp_extended(*extra: bytes, flags: int = 0) -> bytes:
    """An extended (VP8X) WebP with a VP8L image chunk: refused by the media check."""
    vp8x = riff_chunk(b"VP8X", bytes([flags, 0, 0, 0]) + b"\x00\x00\x00\x00\x00\x00")
    return riff(b"WEBP", vp8x, WEBP_IMAGE, *extra)


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


def hdlr(handler: bytes) -> bytes:
    return box(b"hdlr", bytes(8) + handler + bytes(12) + b"\x00")


def stbl(samples: int = 1) -> bytes:
    """A sample table: one sample description, `samples` samples of 16 bytes in one chunk."""
    stsd = box(b"stsd", struct.pack(">II", 0, 1) + box(b"avc1", bytes(78)))
    stts = box(b"stts", struct.pack(">IIII", 0, 1, samples, 1))
    stsc = box(b"stsc", struct.pack(">IIIII", 0, 1, 1, samples, 1))
    stsz = box(b"stsz", struct.pack(">III", 0, 16, samples))
    stco = box(b"stco", struct.pack(">III", 0, 1, 0))
    return box(b"stbl", stsd + stts + stsc + stsz + stco)


def trak(handler: bytes, *, udta: bool = True) -> bytes:
    """A track: tkhd, mdia (mdhd, hdlr, minf with a sample table), and a udta with a name."""
    header = box(b"vmhd", bytes(12)) if handler == b"vide" else box(b"smhd", bytes(8))
    minf = box(b"minf", header + stbl())
    mdia = box(b"mdia", box(b"mdhd", bytes(24)) + hdlr(handler) + minf)
    return box(b"trak", box(b"tkhd", bytes(84)) + mdia + (box(b"udta", box(b"name", GPS)) if udta else b""))


def mp4(
    brand: bytes = b"isom", *, mdat: bytes = b"\x00" * 64, udta: bool = True, handlers: tuple[bytes, ...] = ()
) -> bytes:
    """ftyp, moov (mvhd, a trak per handler with a udta, a udta with a location), mdat. The
    handlers default to a sound track for an audio brand, else a video and a sound track."""
    handlers = handlers or ((b"soun",) if brand in (b"M4A ", b"M4B ") else (b"vide", b"soun"))
    ftyp = box(b"ftyp", brand + b"\x00\x00\x02\x00" + brand + b"mp41")
    location = box(b"udta", box(b"\xa9xyz", b"\x00\x12\x15\xc7" + GPS)) if udta else b""
    tracks = trak(handlers[0], udta=udta) + b"".join(trak(h, udta=False) for h in handlers[1:])
    moov = box(b"moov", box(b"mvhd", bytes(100)) + tracks + location)
    return ftyp + moov + box(b"mdat", mdat)


def ebml(element: bytes, data: bytes, *, unknown: bool = False) -> bytes:
    if unknown:
        return element + b"\x01\xff\xff\xff\xff\xff\xff\xff" + data
    return element + b"\x01" + len(data).to_bytes(7, "big") + data


def webm(doctype: bytes = b"webm", *, live: bool = False, extra: bytes = b"") -> bytes:
    header = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x86", b"\x01") + ebml(b"\x42\x82", doctype))
    info = ebml(b"\x15\x49\xa9\x66", ebml(b"\x2a\xd7\xb1", b"\x0f\x42\x40"))
    entry = ebml(b"\xd7", b"\x01") + ebml(b"\x83", b"\x01") + ebml(b"\x86", b"V_VP8")
    tracks = ebml(b"\x16\x54\xae\x6b", ebml(b"\xae", entry))
    cluster = ebml(
        b"\x1f\x43\xb6\x75", ebml(b"\xe7", b"\x00") + ebml(b"\xa3", b"\x81\x00\x00\x80" + bytes(16)), unknown=live
    )
    return header + ebml(b"\x18\x53\x80\x67", info + tracks + extra + cluster, unknown=live)


def el(element: int, data: bytes) -> bytes:
    """An EBML element by its numeric id, with an 8-byte size."""
    return ebml(element.to_bytes((element.bit_length() + 7) // 8, "big"), data)


MKV_TITLE = b"Secret meeting with the bank"
MKV_TRACK_NAME = b"Front camera of the flat"


def mkv_video_entry(*extra: bytes, codec: bytes = b"V_MPEG4/ISO/AVC", track_type: int = 1) -> bytes:
    """An H.264 video track entry as OBS (through its ffmpeg muxer) writes it."""
    avcc = b"\x01\x64\x00\x1f\xff\xe1\x00\x04\x67\x64\x00\x1f\x01\x00\x04\x68\xeb\xe3\xcb"
    video = el(0xB0, b"\x07\x80") + el(0xBA, b"\x04\x38") + el(0x55B0, el(0x55B1, b"\x01") + el(0x55BB, b"\x01"))
    return el(
        0xAE,
        el(0xD7, b"\x01")
        + el(0x73C5, bytes(range(1, 9)))
        + el(0x9C, b"\x00")
        + el(0x22B59C, b"und")
        + el(0x86, codec)
        + el(0x63A2, avcc)
        + el(0x83, bytes([track_type]))
        + el(0x23E383, (16_666_667).to_bytes(4, "big"))
        + el(0x536E, MKV_TRACK_NAME)
        + el(0xE0, video)
        + b"".join(extra),
    )


def mkv_audio_entry(*extra: bytes, codec: bytes = b"A_AAC") -> bytes:
    audio = el(0xB5, struct.pack(">d", 48000.0)) + el(0x9F, b"\x02") + el(0x6264, b"\x10")
    return el(
        0xAE,
        el(0xD7, b"\x02")
        + el(0x73C5, bytes(range(9, 17)))
        + el(0x86, codec)
        + el(0x63A2, b"\x11\x90")
        + el(0x83, b"\x02")
        + el(0x22B59C, b"eng")
        + el(0xE1, audio)
        + b"".join(extra),
    )


def mkv(
    doctype: bytes = b"matroska",
    *,
    info: bytes = b"",
    entries: bytes | None = None,
    extra: bytes = b"",
    live: bool = False,
    cluster: bytes | None = None,
    crc: bytes | None = None,
) -> bytes:
    """A short recording shaped like OBS's MKV output (H.264 and AAC, ffmpeg's muxer): SeekHead,
    Info with muxing and writing application and a title, two tracks, tags, a cluster and cues."""
    header = el(
        0x1A45DFA3,
        el(0x4286, b"\x01")
        + el(0x42F7, b"\x01")
        + el(0x42F2, b"\x04")
        + el(0x42F3, b"\x08")
        + el(0x4282, doctype)
        + el(0x4287, b"\x04")
        + el(0x4285, b"\x02"),
    )
    segment_info = el(
        0x1549A966,
        (b"" if crc is None else el(0xBF, crc))
        + el(0x2AD7B1, b"\x0f\x42\x40")
        + el(0x4D80, b"Lavf60.16.100")
        + el(0x5741, b"OBS Studio 30.2.3")
        + el(0x7BA9, MKV_TITLE)
        + el(0x4461, bytes(8))
        + el(0x4489, struct.pack(">d", 2000.0))
        + info,
    )
    tracks = el(0x1654AE6B, mkv_video_entry() + mkv_audio_entry() if entries is None else entries)
    tags = el(0x1254C367, el(0x7373, el(0x67C8, el(0x45A3, b"ENCODER") + el(0x4487, b"Lavf60 secret"))))
    timestamp = el(0xE7, b"\x00")
    blocks = timestamp + el(0xA3, b"\x81\x00\x00\x80" + bytes(32)) + el(0xA3, b"\x82\x00\x00\x80" + bytes(8))
    body = ebml(b"\x1f\x43\xb6\x75", blocks if cluster is None else cluster, unknown=live)
    # Every pointer is real, as ffmpeg writes them: the SeekHead names Info, Tracks, Tags and Cues
    # at their offsets in the segment, the cue its cluster and the first block in it.
    targets = [(0x1549A966, segment_info), (0x1654AE6B, tracks), (0x1254C367, tags)]
    seek = seekhead([(element, 0) for element, _ in targets] + ([] if live else [(0x1C53BB6B, 0)]))
    offsets = [len(seek)]
    for _, part in targets:
        offsets.append(offsets[-1] + len(part))
    cluster_at = offsets[-1] + len(extra)
    cues_at = cluster_at + len(body)
    entries_at = [(element, offset) for (element, _), offset in zip(targets, offsets, strict=False)]
    seek = seekhead(entries_at + ([] if live else [(0x1C53BB6B, cues_at)]))
    relative = b"" if cluster is not None else el(0xF0, len(timestamp).to_bytes(8, "big"))
    cue = el(0xB7, el(0xF7, b"\x01") + el(0xF1, cluster_at.to_bytes(8, "big")) + relative)
    cues = el(0x1C53BB6B, el(0xBB, el(0xB3, b"\x00") + cue))
    segment = seek + segment_info + tracks + tags + extra + body
    if not live:
        segment += cues
    return header + ebml(b"\x18\x53\x80\x67", segment, unknown=live)


def seekhead(entries: list[tuple[int, int]]) -> bytes:
    """A SeekHead of (element id, segment-relative position) entries, positions as 8-byte numbers
    (so the size does not depend on them)."""
    return el(
        0x114D9B74,
        b"".join(
            el(0x4DBB, el(0x53AB, element.to_bytes(4, "big")) + el(0x53AC, position.to_bytes(8, "big")))
            for element, position in entries
        ),
    )


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
    "movie.mkv": ("video", "video/x-matroska", mkv()),
    "live.mkv": ("video", "video/x-matroska", mkv(live=True)),
    "vp9.mkv": ("video", "video/x-matroska", webm(b"matroska")),
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
    "movie.webm": webm(b"matroska"),
    "obs.webm": mkv(b"webm"),
    "clip.mkv": webm(),
    "video.ogg": ogg(b"\x80theora" + bytes(34)),
    "film.mp4": mp4(b"3gp4"),
    "empty.png": b"",
}
"""Not media, or media of a type that is not allowed, whatever the name says."""


# --- attacks (red-team review) ---------------------------------------------------------------

SECRET = b"AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
"""Stands for a file a payload smuggles out: it must never reach the client readable."""


def zip_of_secret(*, cover: int = 0) -> bytes:
    """A real ZIP holding `.env` (SECRET). `cover` sets the end record's comment length, so the
    archive still opens when the container appends that many bytes after it (a PNG chunk's CRC
    and IEND, for example)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(".env", SECRET)
    data = bytearray(buffer.getvalue())
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<H", data, end + 20, cover)
    return bytes(data)


def zip_secret(data: bytes | bytearray) -> bytes | None:
    """`.env` from a ZIP anywhere in `data` (as a ZIP reader finds it from the end), else None."""
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            return archive.read(".env")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return None


def apng(frame_data: bytes | None = None) -> bytes:
    """A 1x1 animated PNG: acTL, the default image (fcTL + IDAT) and a second frame (fcTL + fdAT)."""
    header = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    actl = png_chunk(b"acTL", struct.pack(">II", 2, 0))

    def fctl(sequence: int) -> bytes:
        return png_chunk(b"fcTL", struct.pack(">IIIIIHHBB", sequence, 1, 1, 0, 0, 1, 10, 0, 0))

    pixels = zlib.compress(b"\x00\xff\x00\x00")
    second = pixels if frame_data is None else frame_data
    return (
        b"\x89PNG\r\n\x1a\n"
        + header
        + actl
        + fctl(0)
        + png_chunk(b"IDAT", pixels)
        + fctl(1)
        + png_chunk(b"fdAT", struct.pack(">I", 2) + second)
        + png_chunk(b"IEND", b"")
    )


def png_sized(width: int, height: int) -> bytes:
    header = png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    idat = png_chunk(b"IDAT", zlib.compress(bytes((1 + width) * height)))
    return b"\x89PNG\r\n\x1a\n" + header + idat + png_chunk(b"IEND", b"")


def ebml_short(element: bytes, data: bytes) -> bytes:
    """An EBML element with a one-byte size (at most 126 bytes of data)."""
    return element + bytes([0x80 | len(data)]) + data


def mkv_head(block_bytes: int = 0) -> bytes:
    """The start of a live MKV (unknown-size segment and cluster) up to a SimpleBlock header for
    `block_bytes` of frame data: the file is valid once that many bytes (zeros, say) follow. Large
    test files are made from it as sparse files instead of fixtures."""
    head = mkv(live=True, cluster=el(0xE7, b"\x00"))
    return head + b"\xa3\x01" + (4 + block_bytes).to_bytes(7, "big") + b"\x81\x00\x00\x80"


def mkv_sized(total: int) -> bytes:
    """A valid live MKV of exactly `total` bytes (one SimpleBlock of zeros)."""
    head = mkv_head(0)
    head = mkv_head(total - len(head))
    return head + bytes(total - len(head))


def write_sparse_mkv(path: str, total: int) -> None:
    """A valid MKV of exactly `total` bytes as a sparse file (the frame data is a hole)."""
    head = mkv_head(0)
    head = mkv_head(total - len(head))
    with open(path, "wb") as handle:
        handle.write(head)
        handle.truncate(total)
