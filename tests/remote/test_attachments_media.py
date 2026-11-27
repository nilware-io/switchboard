"""`dispatcher.media_check`: only whole image, audio and video files pass, decided by magic bytes
and a walk of the whole container, with metadata stripped where that is cheap and safe."""

from __future__ import annotations

import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path

import pytest

from tests.remote.media_fixtures import (
    GPS,
    HTML,
    MKV_TITLE,
    MKV_TRACK_NAME,
    NOT_MEDIA,
    SECRET,
    SVG,
    VALID,
    ZIP,
    apng,
    box,
    crc_bitwise,
    ebml,
    ebml_short,
    el,
    exif_segment,
    flac,
    flac_block,
    gif,
    gif_comment,
    hdlr,
    icc_profile,
    jpeg,
    jpeg_segment,
    mkv,
    mkv_audio_entry,
    mkv_video_entry,
    mp3,
    mp4,
    ogg,
    png,
    png_chunk,
    png_sized,
    riff,
    riff_chunk,
    seekhead,
    stbl,
    trak,
    wav,
    webm,
    webp,
    webp_extended,
    zip_of_secret,
    zip_secret,
)
from voice_coding_mcp.remote import dispatcher as d


def refused(name: str, data: bytes | bytearray) -> str:
    with pytest.raises(d.RefusedError) as caught:
        d.media_check(name, data)
    return caught.value.code


@pytest.mark.parametrize("name", sorted(VALID))
def test_every_allowed_format_passes_with_its_kind_and_type(name: str) -> None:
    kind, mime, data = VALID[name]
    got_kind, got_mime, cleaned = d.media_check(name, data)
    assert (got_kind, got_mime) == (kind, mime)
    # Checking the cleaned bytes again changes nothing.
    assert d.media_check(name, cleaned) == (kind, mime, cleaned)


@pytest.mark.parametrize("name", sorted(VALID))
def test_anything_after_the_container_or_a_cut_file_is_refused(name: str) -> None:
    data = VALID[name][2]
    assert refused(name, data + ZIP) == "not_media"
    assert refused(name, data + b"\x00\x01") == "not_media"
    assert refused(name, data[:-3]) == "not_media"
    if not name.endswith(".flac"):
        # FLAC frames are not decoded: only the CRC-16 of the last frame shows where the file ends,
        # and a CRC with initial value 0 cannot see zero bytes appended to it (documented).
        assert refused(name, data + b"\x00") == "not_media"


def test_upper_case_extensions_count() -> None:
    assert d.media_check("SHOT.PNG", png())[0] == "image"


@pytest.mark.parametrize("name", sorted(NOT_MEDIA))
def test_other_types_are_refused_whatever_the_name(name: str) -> None:
    assert refused(name, NOT_MEDIA[name]) == "not_media"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("shot.jpg", png()),
        ("photo.png", jpeg()),
        ("take.wav", VALID["pic.webp"][2]),
        ("clip.mp4", mp4(b"M4A ")),
        ("song.m4a", mp4()),
        ("clip.mov", mp4()),
        ("voice.flac", mp3()),
        ("clip.webm", ogg()),
        ("shot", png()),
        ("shot.png.svg", png()),
        (".shot.png", png()),
        ("-shot.png", png()),
        ("../shot.png", png()),
    ],
)
def test_the_extension_must_agree_with_the_magic_bytes(name: str, data: bytes) -> None:
    assert refused(name, data) == "not_media"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("shot.png", png() + ZIP),
        ("photo.jpg", jpeg() + HTML),
        ("anim.gif", gif() + b"*/=1;alert(document.domain)//"),
        ("anim.gif", b"GIF89a/*" + gif()[8:] + b"*/=alert(1)"),
        ("clip.mp4", mp4() + b"trailing data"),
        ("clip.mp4", mp4() + ZIP),
        ("take.wav", wav() + HTML),
        ("take.mp3", mp3() + SVG),
        ("voice.opus", ogg() + HTML),
        ("take.flac", flac() + ZIP),
        ("clip.webm", webm() + HTML),
        ("pic.webp", webp() + ZIP),
    ],
)
def test_polyglots_are_refused(name: str, data: bytes) -> None:
    assert refused(name, data) == "not_media"


def test_structure_errors_are_refused() -> None:
    bad_crc = png()[:-5] + b"\x00" + png()[-4:]
    assert refused("shot.png", bad_crc) == "not_media"
    assert refused("shot.png", png()[:8] + png_chunk(b"IEND", b"")) == "not_media"
    no_scan = b"\xff\xd8" + jpeg_segment(0xE0, b"JFIF\x00") + b"\xff\xd9"
    assert refused("photo.jpg", no_scan) == "not_media"
    assert refused("anim.gif", gif()[:-6] + b"\x3b") == "not_media"
    riff_size = webp()[:4] + struct.pack("<I", 99) + webp()[8:]
    assert refused("pic.webp", riff_size) == "not_media"
    assert refused("pic.webp", webp(riff_chunk(b"ZZZZ", b"x"))) == "not_media"
    assert refused("take.wav", wav()[:12] + riff_chunk(b"data", b"\x00\x00") * 1) == "not_media"
    page = bytearray(ogg())
    page[30] ^= 0xFF
    assert refused("voice.opus", bytes(page)) == "not_media"
    frame = bytearray(flac())
    frame[-1] ^= 0xFF
    assert refused("take.flac", bytes(frame)) == "not_media"
    assert refused("take.mp3", b"\xff\xfb\x00\x64" + bytes(413)) == "not_media"
    assert refused("clip.mp4", mp4()[: mp4().index(b"moov") - 4] + mp4()[-72:]) == "not_media"
    attachments = webm(extra=b"\x19\x41\xa4\x69\x81\x00")
    assert refused("clip.webm", attachments) == "not_media"


def test_exif_gps_and_text_are_stripped_from_images() -> None:
    comment = jpeg_segment(0xFE, b"made at " + GPS)
    _, _, clean = d.media_check("photo.jpg", jpeg(exif_segment(), comment))
    assert GPS not in clean
    assert b"Exif" not in clean
    assert clean == jpeg()
    chunks = (png_chunk(b"tEXt", b"Comment\x00" + GPS), png_chunk(b"eXIf", b"MM\x00\x2a" + GPS))
    _, _, clean = d.media_check("shot.png", png(*chunks))
    assert GPS not in clean
    assert clean == png()
    _, _, clean = d.media_check("anim.gif", gif(gif_comment(GPS)))
    assert clean == gif()
    with_exif = webp_extended(riff_chunk(b"EXIF", b"MM\x00\x2a" + GPS), riff_chunk(b"XMP ", GPS), flags=0x0C)
    assert refused("pic.webp", with_exif) == "not_media"


def test_tags_are_stripped_from_mp3_wav_flac_and_mp4() -> None:
    _, _, clean = d.media_check("take.mp3", mp3(id3=b"TIT2" + GPS, id3v1=True))
    assert clean == mp3()
    _, _, clean = d.media_check("take.wav", wav(riff_chunk(b"LIST", b"INFO" + GPS)))
    assert clean == wav()
    tags = flac_block(4, b"\x00" * 8 + GPS)
    picture = flac_block(6, b"\x00" * 32 + GPS)
    _, _, clean = d.media_check("take.flac", flac(tags, picture))
    assert clean == flac()
    for name, brand in (("clip.mp4", b"isom"), ("clip.mov", b"qt  "), ("song.m4a", b"M4A ")):
        original = mp4(brand)
        _, _, clean = d.media_check(name, original)
        assert GPS not in clean
        # Same size and offsets: the boxes became zeroed `free` boxes.
        assert len(clean) == len(original)
        assert clean.count(b"free") == 2


def test_ogg_comments_are_left_as_they_are() -> None:
    # Documented: Vorbis and Opus comments are not stripped.
    data = ogg()
    assert d.media_check("voice.opus", data)[2] == data
    assert d.media_check("clip.webm", webm())[2] == webm()


def test_size_caps_per_kind() -> None:
    big = png(png_chunk(b"zzZz", bytes(d.ATTACHMENT_CAPS["image"])))
    assert refused("shot.png", big) == "too_large"
    assert d.media_check("shot.png", png(png_chunk(b"zzZz", bytes(d.ATTACHMENT_CAPS["image"] - 200))))
    # 250,000,001 bytes: refused on the length alone, before any walk (and before any copy).
    video = bytearray(d.ATTACHMENT_CAPS["video"] + 1)
    video[:64] = mp4()[:64]
    assert refused("clip.mp4", video) == "too_large"
    video[: len(mkv())] = mkv()
    assert refused("movie.mkv", video) == "too_large"
    assert refused("take.wav", wav() + bytes(d.ATTACHMENT_CAPS["audio"])) == "too_large"
    # An image as base64 stays within the 5 MB the Claude API accepts per image.
    assert 4 * -(-d.ATTACHMENT_CAPS["image"] // 3) <= 5_000_000


def test_checksums_match_bitwise_reference() -> None:
    sample = bytes(range(256)) * 3 + b"OggS"
    assert d.ogg_crc(sample) == crc_bitwise(sample, 0x04C11DB7, 32)
    assert d.flac_crc8(sample) == crc_bitwise(sample, 0x07, 8)
    assert d.flac_crc16(sample) == crc_bitwise(sample, 0x8005, 16)


def test_sniff_reads_only_magic_bytes() -> None:
    for name, (_, _, data) in VALID.items():
        assert d.sniff_media(data[: d.SNIFF_BYTES]) == d.expected_format(name), name
    assert d.sniff_media(SVG) is None
    assert d.sniff_media(HTML) is None
    assert d.sniff_media(ZIP) is None


# --- attacks from the red-team review ------------------------------------------------------------
#
# Each of these got a payload (a ZIP of a secret, a PDF, HTML) or a slow walk through the check
# before the allowlists, the track requirements and the element budget.

FTYP = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
MOOV = box(b"moov", box(b"mvhd", bytes(100)) + trak(b"vide", udta=False))
MDAT = box(b"mdat", bytes(64))


def test_mp4_needs_a_real_track_and_media_data() -> None:
    # An empty moov in front of a free box holding a ZIP was accepted as a video.
    assert refused("x.mp4", FTYP + box(b"moov", b"") + box(b"free", zip_of_secret())) == "not_media"
    assert refused("x.mp4", FTYP + box(b"moov", box(b"mvhd", bytes(100))) + MDAT) == "not_media"
    no_handler = box(b"moov", box(b"mvhd", bytes(100)) + box(b"trak", box(b"tkhd", bytes(84))))
    assert refused("x.mp4", FTYP + no_handler + MDAT) == "not_media"
    text_track = box(b"moov", box(b"mvhd", bytes(100)) + trak(b"text", udta=False))
    assert refused("x.mp4", FTYP + text_track + MDAT) == "not_media"
    assert refused("x.mp4", FTYP + MOOV) == "not_media"
    assert d.media_check("x.mp4", FTYP + MOOV + MDAT)[0] == "video"


def test_mp4_padding_and_private_boxes_are_zeroed() -> None:
    for kind in (b"free", b"skip", b"wide", b"uuid"):
        data = FTYP + MOOV + MDAT + box(kind, zip_of_secret())
        _, _, clean = d.media_check("x.mp4", data)
        assert len(clean) == len(data)
        assert SECRET not in clean
        assert zip_secret(clean) is None
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    _, _, clean = d.media_check("x.mp4", FTYP + box(b"free", pdf) + MOOV + MDAT)
    assert b"%PDF" not in clean
    assert b"%%EOF" not in clean


def test_mp4_unknown_boxes_and_open_ended_boxes_are_refused() -> None:
    assert refused("x.mp4", FTYP + MOOV + MDAT + box(b"zzzz", SECRET)) == "not_media"
    assert refused("x.mp4", FTYP + MOOV + MDAT + struct.pack(">I", 0) + b"zzzz" + SECRET) == "not_media"
    assert refused("x.mp4", FTYP + MOOV + struct.pack(">I", 0) + b"free" + SECRET) == "not_media"
    # Only mdat may run to the end of the file.
    assert d.media_check("x.mp4", FTYP + MOOV + struct.pack(">I", 0) + b"mdat" + bytes(64))[0] == "video"
    in_moov = box(b"moov", box(b"mvhd", bytes(100)) + trak(b"vide", udta=False) + box(b"zzzz", SECRET))
    assert refused("x.mp4", FTYP + in_moov + MDAT) == "not_media"
    stbl = box(b"stbl", box(b"zzzz", SECRET))
    minf = box(b"minf", box(b"vmhd", bytes(12)) + stbl)
    deep = box(b"trak", box(b"tkhd", bytes(84)) + box(b"mdia", box(b"mdhd", bytes(24)) + hdlr(b"vide") + minf))
    assert refused("x.mp4", FTYP + box(b"moov", box(b"mvhd", bytes(100)) + deep) + MDAT) == "not_media"
    assert refused("x.mp4", FTYP + MOOV + MDAT + box(b"ftyp", b"isom\x00\x00\x02\x00")) == "not_media"


def test_mp4_audio_or_video_comes_from_the_tracks() -> None:
    # An Apple .m4v lists M4A among its compatible brands: still a video.
    m4v = box(b"ftyp", b"M4V \x00\x00\x00\x01M4A M4V mp42isom") + MOOV + MDAT
    assert d.media_check("film.m4v", m4v)[:2] == ("video", "video/mp4")
    # A generic brand with only a sound track is an .m4a (ffmpeg's mp4 muxer writes these).
    generic = mp4(b"isom", handlers=(b"soun",))
    assert d.media_check("song.m4a", generic)[:2] == ("audio", "audio/mp4")
    assert refused("clip.mp4", generic) == "not_media"
    assert refused("song.m4a", mp4(b"M4A ", handlers=(b"soun", b"vide"))) == "not_media"


def test_png_keeps_only_known_chunks() -> None:
    # A private chunk holding a ZIP whose comment covers the CRC and IEND behind it.
    iend = png_chunk(b"IEND", b"")
    data = png()[: -len(iend)] + png_chunk(b"zIPp", zip_of_secret(cover=4 + len(iend))) + iend
    assert zip_secret(data) == SECRET
    _, _, clean = d.media_check("x.png", data)
    assert clean == png()
    for kind in (b"exIf", b"vpAg", b"caNv", b"tIME", b"oFFs"):
        assert d.media_check("x.png", png(png_chunk(kind, GPS)))[2] == png()


def test_png_image_data_must_inflate_to_exactly_the_scanlines() -> None:
    def with_idat(payload: bytes) -> bytes:
        header = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        return b"\x89PNG\r\n\x1a\n" + header + png_chunk(b"IDAT", payload) + png_chunk(b"IEND", b"")

    assert refused("x.png", with_idat(SECRET)) == "not_media"
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    assert refused("x.png", with_idat(pixels + SECRET)) == "not_media"
    assert refused("x.png", with_idat(zlib.compress(b"\x00\xff\x00\x00" + SECRET))) == "not_media"
    assert refused("x.png", with_idat(zlib.compress(b"\x00\xff\x00"))) == "not_media"
    assert refused("x.png", with_idat(pixels[:-4])) == "not_media"
    assert d.media_check("x.png", with_idat(pixels))[0] == "image"
    # Animation frames are inflated too.
    assert d.media_check("x.png", apng())[0] == "image"
    assert refused("x.png", apng(SECRET)) == "not_media"


def test_images_over_8000_pixels_a_side_are_refused() -> None:
    assert d.media_check("x.png", png_sized(8000, 1))[0] == "image"
    assert refused("x.png", png_sized(8001, 1)) == "not_media"
    assert refused("x.png", png_sized(1, 8001)) == "not_media"
    sof = jpeg_segment(0xC0, b"\x08\x00\x01\x1f\x41\x01\x01\x11\x00")
    wide = jpeg().replace(jpeg_segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"), sof)
    assert refused("x.jpg", wide) == "not_media"
    screen = gif()
    assert refused("x.gif", screen[:6] + struct.pack("<H", 9000) + screen[8:]) == "not_media"


def test_jpeg_keeps_only_known_segments() -> None:
    html = b"<html><script>alert(1)</script></html>"
    for marker in (0xE2, 0xE3, 0xEB, 0xEF):
        assert d.media_check("x.jpg", jpeg(jpeg_segment(marker, html + SECRET)))[2] == jpeg()
    # APP0 that is not JFIF and APP14 that is not Adobe are dropped as well.
    assert d.media_check("x.jpg", jpeg(jpeg_segment(0xE0, html)))[2] == jpeg()
    assert d.media_check("x.jpg", jpeg(jpeg_segment(0xEE, html)))[2] == jpeg()
    icc = jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + icc_profile())
    adobe = jpeg_segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01")
    assert d.media_check("x.jpg", jpeg(icc, adobe))[2] == jpeg(icc, adobe)
    # Markers outside the standard set are refused.
    for marker in (0xF0, 0xFD, 0xC8, 0x02):
        assert refused("x.jpg", jpeg(jpeg_segment(marker, SECRET))) == "not_media"


def test_jpeg_keeps_only_the_exif_orientation() -> None:
    ifd = struct.pack(">H", 3)
    ifd += struct.pack(">HHIHH", 0x010F, 2, 4, 0, 0)
    ifd += struct.pack(">HHIHH", 0x0112, 3, 1, 6, 0)
    ifd += struct.pack(">HHII", 0x8825, 4, 1, 0)
    exif = jpeg_segment(0xE1, b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08" + ifd + bytes(4) + GPS)
    _, _, clean = d.media_check("photo.jpg", jpeg(exif))
    assert GPS not in clean
    assert clean == jpeg(d.orientation_segment(6))
    little = jpeg_segment(
        0xE1,
        b"Exif\x00\x00II\x2a\x00\x08\x00\x00\x00\x01\x00\x12\x01\x03\x00\x01\x00\x00\x00\x08\x00\x00\x00" + bytes(4),
    )
    assert d.media_check("photo.jpg", jpeg(little))[2] == jpeg(d.orientation_segment(8))
    # Orientation 1 (upright) or none: no EXIF at all.
    upright = jpeg_segment(
        0xE1, b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08\x00\x01\x01\x12\x00\x03\x00\x00\x00\x01\x00\x01\x00\x00"
    )
    assert d.media_check("photo.jpg", jpeg(upright))[2] == jpeg()
    assert d.media_check("photo.jpg", jpeg(exif_segment()))[2] == jpeg()


def test_wav_gif_and_ogg_side_channels() -> None:
    assert d.media_check("x.wav", wav(riff_chunk(b"zzzz", SECRET), riff_chunk(b"bext", GPS)))[2] == wav()
    plain_text = b"\x21\x01\x0c" + bytes(12) + bytes([len(SECRET) % 255]) + SECRET[:255] + b"\x00"
    assert d.media_check("x.gif", gif(plain_text))[2] == gif()
    # A second logical stream (a multiplexed video or data stream) is refused.
    stream = ogg()
    other = bytearray(stream[-30:])
    page = bytearray(stream)
    page[-len(other) + 14 : -len(other) + 18] = b"\x99\x99\x99\x99"
    assert refused("x.opus", bytes(page)) == "not_media"


def test_webm_needs_a_track_and_blanks_voids_tags_and_chapters() -> None:
    header = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x82", b"webm"))
    empty_tracks = ebml_short(b"\x16\x54\xae\x6b", b"")
    assert refused("x.webm", header + ebml(b"\x18\x53\x80\x67", empty_tracks)) == "not_media"
    entry = ebml_short(b"\xd7", b"\x01") + ebml_short(b"\x83", b"\x01") + ebml_short(b"\x86", b"V_VP9")
    tracks = ebml_short(b"\x16\x54\xae\x6b", ebml_short(b"\xae", entry))
    void = ebml_short(b"\xec", SECRET)
    tags = ebml_short(b"\x12\x54\xc3\x67", GPS)
    chapters = ebml(b"\x10\x43\xa7\x70", GPS)
    cluster = ebml(b"\x1f\x43\xb6\x75", ebml_short(b"\xe7", b"\x00") + ebml_short(b"\xec", SECRET))
    data = header + ebml(b"\x18\x53\x80\x67", tracks + void + tags + chapters + cluster)
    kind, _, clean = d.media_check("x.webm", data)
    assert kind == "video"
    assert len(clean) == len(data)
    assert SECRET not in clean
    assert GPS not in clean
    # The blanked elements are Voids: checking the result again changes nothing.
    assert d.media_check("x.webm", clean)[2] == clean
    assert d.ebml_void(2) == b"\xec\x80"
    assert d.ebml_void(12) == b"\xec\x01" + bytes(6) + b"\x03" + bytes(3)
    header_void = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x82", b"webm") + ebml_short(b"\xec", SECRET))
    _, _, clean = d.media_check("x.webm", header_void + ebml(b"\x18\x53\x80\x67", tracks + cluster))
    assert SECRET not in clean
    unknown_in_header = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x82", b"webm") + ebml_short(b"\x42\x99", SECRET))
    assert refused("x.webm", unknown_in_header + ebml(b"\x18\x53\x80\x67", tracks + cluster)) == "not_media"
    long_crc = ebml_short(b"\xbf", SECRET)
    assert refused("x.webm", header + ebml(b"\x18\x53\x80\x67", tracks + long_crc + cluster)) == "not_media"
    assert refused("x.webm", header + ebml(b"\x18\x53\x80\x67", ebml_short(b"\x16\x54\xae\x6b", SECRET))) == (
        "not_media"
    )


# --- the walks stay fast: pure Python, run by the hub as well -------------------------------------

FAST_SECONDS = 2.0
"""Generous: every case below takes well under half a second on a laptop."""


def timed(name: str, data: bytes) -> tuple[float, str | None]:
    started = time.monotonic()
    try:
        d.media_check(name, data)
        code = None
    except d.RefusedError as error:
        code = error.code
    return time.monotonic() - started, code


VIDEO_SAMPLE = 16 * 1024 * 1024
"""Video size of the tiny-element cases: the element budget, not the 250 MB cap, bounds them."""


def test_millions_of_tiny_elements_are_refused_fast() -> None:
    doctype = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x82", b"webm"))
    tracks = ebml_short(b"\x16\x54\xae\x6b", ebml_short(b"\xae", b""))
    voids = b"\xec\x80" * ((VIDEO_SAMPLE - 100) // 2)
    cases = {
        "x.webm": doctype + b"\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff" + tracks + voids,
        "x.mp4": FTYP + MOOV + MDAT + box(b"free", b"") * ((VIDEO_SAMPLE - 1000) // 8),
        "x.wav": wav(*([riff_chunk(b"abcd", b"")] * ((d.ATTACHMENT_CAPS["audio"] - 1000) // 8))),
        "x.gif": gif()[:-1][:-6] + b"\x02" + b"\x01\x00" * 1_800_000 + b"\x00\x3b",
        "x.mp3": (b"\xff\xf3\x10\xc4" + bytes(22)) * ((d.ATTACHMENT_CAPS["audio"] - 1000) // 26),
    }
    for name, data in cases.items():
        seconds, code = timed(name, data)
        assert code == "not_media", name
        assert seconds < FAST_SECONDS, (name, seconds)


def test_flac_last_frame_search_has_a_budget() -> None:
    header = b"\xff\xf8\x19\x18\x00"
    header += bytes([d.flac_crc8(header)])
    info = flac_block(0, bytes(34), last=True)
    body = bytearray(d.ATTACHMENT_CAPS["audio"] - 64)
    body[: len(header)] = header
    step = (d.FLAC_MAX_FRAME - 4096) // 70
    for index in range(70):
        pos = len(body) - 4 - index * step
        body[pos - len(header) : pos] = header
    seconds, code = timed("x.flac", b"fLaC" + info + bytes(body))
    assert code == "not_media"
    assert seconds < FAST_SECONDS
    seconds, code = timed("x.flac", b"fLaC" + info + header + b"\xff\xf8" * (d.FLAC_MAX_FRAME // 2))
    assert code == "not_media"
    assert seconds < FAST_SECONDS


def test_large_valid_files_pass_fast() -> None:
    stuffed = jpeg(scan=b"\xff\x00" * ((d.ATTACHMENT_CAPS["image"] - 300) // 2))
    for name, data in (("x.jpg", stuffed), ("x.png", png_sized(8000, 4000))):
        seconds, code = timed(name, data)
        assert code is None, name
        assert seconds < FAST_SECONDS, (name, seconds)


# --- red team, round 2: allowlisted names carrying extra bytes ----------------------------------


def png_before_iend(*chunks: bytes) -> bytes:
    iend = png_chunk(b"IEND", b"")
    return png()[: -len(iend)] + b"".join(chunks) + iend


@pytest.mark.parametrize("kind", [k for k in d.PNG_KEEP if k not in (b"IHDR", b"IDAT", b"IEND")])
def test_png_a_zip_in_any_kept_chunk_is_refused(kind: bytes) -> None:
    # Round 1's polyglot with the private chunk renamed to an allowlisted one, last before IEND
    # (the ZIP comment covers the CRC and IEND) or before the image data.
    late = png_before_iend(png_chunk(kind, zip_of_secret(cover=16)))
    assert zip_secret(late) == SECRET
    assert refused("x.png", late) == "not_media"
    assert refused("x.png", png(png_chunk(kind, zip_of_secret()))) == "not_media"


def test_png_kept_chunks_need_their_lengths_and_places() -> None:
    good = (
        png_chunk(b"gAMA", struct.pack(">I", 45455)),
        png_chunk(b"cHRM", struct.pack(">8I", 31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000)),
        png_chunk(b"sRGB", b"\x00"),
        png_chunk(b"sBIT", b"\x08\x08\x08"),
        png_chunk(b"bKGD", bytes(6)),
        png_chunk(b"tRNS", bytes(6)),
        png_chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1)),
        png_chunk(b"iCCP", b"display\x00\x00" + zlib.compress(icc_profile())),
    )
    data = png(*good)
    assert d.media_check("x.png", data)[2] == data
    # Twice, after the image data, or not for this color type.
    assert refused("x.png", png(good[0], good[0])) == "not_media"
    assert refused("x.png", png_before_iend(good[0])) == "not_media"
    assert refused("x.png", png(png_chunk(b"bKGD", bytes(2)))) == "not_media"
    assert refused("x.png", png(png_chunk(b"tRNS", bytes(2)))) == "not_media"
    assert refused("x.png", png(png_chunk(b"sRGB", b"\x09"))) == "not_media"
    rgba = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    with_alpha = b"\x89PNG\r\n\x1a\n" + rgba + png_chunk(b"tRNS", bytes(6))
    assert refused("x.png", with_alpha + png()[33:]) == "not_media"
    gray = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    assert refused("x.png", b"\x89PNG\r\n\x1a\n" + gray + png_chunk(b"PLTE", bytes(3)) + png()[33:]) == "not_media"
    # An ICC profile with bytes after its zlib stream, or a stream that is not a profile.
    icc = zlib.compress(icc_profile())
    assert refused("x.png", png(png_chunk(b"iCCP", b"p\x00\x00" + icc + SECRET))) == "not_media"
    assert refused("x.png", png(png_chunk(b"iCCP", b"p\x00\x00" + zlib.compress(SECRET)))) == "not_media"


def test_wav_keeps_only_format_fact_and_data_at_their_sizes() -> None:
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    data = riff_chunk(b"data", bytes(160))
    for kind in (b"smpl", b"cue ", b"LIST", b"zzzz"):
        sneaky = riff(b"WAVE", riff_chunk(b"fmt ", fmt), data, riff_chunk(kind, zip_of_secret()))
        assert zip_secret(sneaky) == SECRET
        _, _, clean = d.media_check("x.wav", sneaky)
        assert clean == wav()
        assert zip_secret(clean) is None
    assert refused("x.wav", riff(b"WAVE", riff_chunk(b"fmt ", fmt + SECRET), data)) == "not_media"
    assert refused("x.wav", riff(b"WAVE", riff_chunk(b"fmt ", fmt + b"\x00\x00" + SECRET), data)) == "not_media"
    short = riff(b"WAVE", riff_chunk(b"fmt ", fmt + b"\x00\x00"), data)
    assert d.media_check("x.wav", short)[2] == short
    guid = struct.pack("<H", 1) + d.WAV_GUID_TAIL
    extensible = struct.pack("<HHIIHHHHI", 0xFFFE, 2, 8000, 32000, 4, 16, 22, 16, 3) + guid
    good = riff(b"WAVE", riff_chunk(b"fmt ", extensible), riff_chunk(b"fact", bytes(4)), data)
    assert d.media_check("x.wav", good)[2] == good
    bad_guid = extensible[:-14] + SECRET[:14]
    assert refused("x.wav", riff(b"WAVE", riff_chunk(b"fmt ", bad_guid), data)) == "not_media"
    long_fact = riff(b"WAVE", riff_chunk(b"fmt ", fmt), riff_chunk(b"fact", SECRET), data)
    assert refused("x.wav", long_fact) == "not_media"
    twice = riff(b"WAVE", riff_chunk(b"fmt ", fmt), data, riff_chunk(b"data", bytes(4)))
    assert refused("x.wav", twice) == "not_media"
    # The pad byte after an odd-sized chunk is zeroed.
    odd = bytearray(riff(b"WAVE", riff_chunk(b"fmt ", fmt), riff_chunk(b"data", bytes(161))))
    odd[-1] = 0x5A
    assert d.media_check("x.wav", bytes(odd))[2][-1] == 0


def mp4_with(
    *, mvhd: bytes = bytes(100), tkhd: bytes = bytes(84), name: bytes = b"\x00", stbl_box: bytes = b""
) -> bytes:
    minf = box(b"minf", box(b"vmhd", bytes(12)) + (stbl_box or stbl()))
    mdia = box(b"mdia", box(b"mdhd", bytes(24)) + box(b"hdlr", bytes(8) + b"vide" + bytes(12) + name) + minf)
    moov = box(b"moov", box(b"mvhd", mvhd) + box(b"trak", box(b"tkhd", tkhd) + mdia))
    return box(b"ftyp", b"isom\x00\x00\x02\x00isommp41") + moov + box(b"mdat", bytes(64))


def test_mp4_leaf_boxes_need_their_sizes() -> None:
    assert d.media_check("x.mp4", mp4_with())[0] == "video"
    assert refused("x.mp4", mp4_with(mvhd=bytes(100) + SECRET)) == "not_media"
    assert refused("x.mp4", mp4_with(tkhd=bytes(84) + SECRET)) == "not_media"
    assert refused("x.mp4", mp4_with(mvhd=b"\x02" + bytes(99))) == "not_media"
    # A handler name holding a ZIP, with moov after mdat so the ZIP ends the file.
    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isommp41")
    late = mp4_with(name=zip_of_secret())
    moov = late[len(ftyp) : -72]
    late = ftyp + box(b"mdat", bytes(64)) + moov
    assert zip_secret(late) == SECRET
    _, _, clean = d.media_check("x.mp4", late)
    assert zip_secret(clean) is None
    assert SECRET not in clean
    assert refused("x.mp4", mp4_with(name=bytes(d.MP4_HANDLER_NAME + 1))) == "not_media"
    # A short name is kept in size but zeroed.
    _, _, clean = d.media_check("x.mp4", mp4_with(name=GPS))
    assert GPS not in clean
    stts = box(b"stts", struct.pack(">II", 0, 0) + SECRET)
    sample_table = stbl()
    assert refused(
        "x.mp4", mp4_with(stbl_box=sample_table.replace(box(b"stts", struct.pack(">IIII", 0, 1, 1, 1)), stts))
    ) == ("not_media")


def test_mp4_times_and_unneeded_boxes_are_zeroed() -> None:
    dated = bytearray(mp4_with())
    mvhd = dated.index(b"mvhd") + 4
    dated[mvhd + 4 : mvhd + 12] = b"\x12\x34\x56\x78\x9a\xbc\xde\xf0"
    _, _, clean = d.media_check("x.mp4", bytes(dated))
    assert clean[mvhd + 4 : mvhd + 12] == bytes(8)
    moov = box(b"moov", box(b"mvhd", bytes(100)) + box(b"iods", SECRET) + trak(b"vide", udta=False))
    _, _, clean = d.media_check("x.mp4", box(b"ftyp", b"isom\x00\x00\x02\x00isommp41") + moov + box(b"mdat", bytes(64)))
    assert SECRET not in clean


def test_mp4_video_track_needs_real_sample_tables() -> None:
    assert refused("x.mp4", mp4_with(stbl_box=box(b"stbl", b""))) == "not_media"
    assert refused("x.mp4", mp4_with(stbl_box=stbl(samples=0))) == "not_media"
    no_offsets = stbl().replace(box(b"stco", struct.pack(">III", 0, 1, 0)), b"")
    assert refused("x.mp4", mp4_with(stbl_box=box(b"stbl", no_offsets[8:]))) == "not_media"


def test_jpeg_kept_app_segments_carry_nothing_extra() -> None:
    app0 = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    thumb = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x02\x02" + bytes(12) + SECRET)
    assert d.media_check("x.jpg", jpeg().replace(app0, thumb))[2] == jpeg()
    assert d.media_check("x.jpg", jpeg(jpeg_segment(0xE0, b"JFXX\x00\x10" + GPS)))[2] == jpeg()
    assert d.media_check("x.jpg", jpeg(jpeg_segment(0xEE, b"Adobe<script>" + SECRET)))[2] == jpeg()
    assert d.media_check("x.jpg", jpeg(jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01<script>" + SECRET)))[2] == jpeg()
    # A profile in two parts is kept; a missing or extra part drops them all.
    profile = icc_profile(300)
    parts = [
        jpeg_segment(0xE2, b"ICC_PROFILE\x00" + bytes([i + 1, 2]) + profile[i * 150 : (i + 1) * 150]) for i in range(2)
    ]
    assert d.media_check("x.jpg", jpeg(*parts))[2] == jpeg(*parts)
    assert d.media_check("x.jpg", jpeg(parts[0]))[2] == jpeg()
    assert d.media_check("x.jpg", jpeg(*parts, parts[1]))[2] == jpeg()


@pytest.mark.parametrize(
    "segment",
    [
        jpeg_segment(0xDB, b"\x00" + bytes(range(1, 65)) + SECRET),
        jpeg_segment(0xC4, b"\x00\x01" + bytes(15) + b"\x00" + SECRET),
        jpeg_segment(0xDD, b"\x00\x10" + SECRET),
    ],
)
def test_jpeg_standard_segments_carry_nothing_extra(segment: bytes) -> None:
    assert refused("x.jpg", jpeg(segment)) == "not_media"


def test_gif_kept_extensions_carry_nothing_extra() -> None:
    def blocks(payload: bytes) -> bytes:
        return (
            b"".join(bytes([len(payload[i : i + 255])]) + payload[i : i + 255] for i in range(0, len(payload), 255))
            + b"\x00"
        )

    assert d.media_check("x.gif", gif(b"\x21\xf9" + blocks(bytes(4) + SECRET)))[2] == gif()
    assert d.media_check("x.gif", gif(b"\x21\xff\x0bNETSCAPE2.0" + blocks(SECRET)))[2] == gif()
    gce = b"\x21\xf9\x04\x00\x0a\x00\x00\x00"
    loop = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    assert d.media_check("x.gif", gif(loop, gce))[2] == gif(loop, gce)


def test_webm_needs_a_video_track() -> None:
    audio_only = webm().replace(
        b"\x83\x01\x00\x00\x00\x00\x00\x00\x01\x01", b"\x83\x01\x00\x00\x00\x00\x00\x00\x01\x02"
    )
    assert audio_only != webm()
    assert refused("x.webm", audio_only) == "not_media"


def test_only_simple_webp_is_accepted() -> None:
    """The extended format's extra chunks (red team, round 3: ZIP data in a last ANIM or ALPH
    chunk, bytes after a VP8X header, EXIF inside an ANMF frame) cannot carry anything, because
    the extended format is refused as a whole."""
    zip_bytes = b"PK\x03\x04" + bytes(26) + b".env"
    assert refused("pic.webp", webp_extended()) == "not_media"
    assert refused("pic.webp", webp_extended(riff_chunk(b"ANIM", zip_bytes), flags=0x02)) == "not_media"
    assert refused("pic.webp", webp_extended(riff_chunk(b"ALPH", zip_bytes), flags=0x10)) == "not_media"
    anmf = riff_chunk(b"ANMF", bytes(16) + riff_chunk(b"EXIF", b"MM\x00\x2a" + GPS))
    assert refused("pic.webp", webp_extended(anmf, flags=0x02)) == "not_media"
    assert refused("pic.webp", webp(riff_chunk(b"VP8L", b"\x2f\x00\x00\x00\x00"))) == "not_media"
    assert refused("pic.webp", webp(riff_chunk(b"ICCP", zip_bytes))) == "not_media"
    _, _, clean = d.media_check("pic.webp", webp())
    assert clean == webp()


# --- Matroska (.mkv, OBS's default) ------------------------------------------------------------


def test_an_obs_mkv_passes_with_its_text_blanked() -> None:
    data = mkv()
    kind, mime, clean = d.media_check("movie.mkv", data)
    assert (kind, mime) == ("video", "video/x-matroska")
    assert len(clean) == len(data)
    for text in (MKV_TITLE, MKV_TRACK_NAME, b"Lavf60.16.100", b"OBS Studio", b"ENCODER", b"secret"):
        assert text not in clean, text
    # What playback needs stays: codecs, their private data, the language, sizes, the blocks.
    for kept in (b"V_MPEG4/ISO/AVC", b"A_AAC", b"\x67\x64\x00\x1f", b"und", b"eng", b"\x0f\x42\x40"):
        assert kept in clean, kept
    assert d.media_check("movie.mkv", clean)[2] == clean
    live = mkv(live=True)
    assert MKV_TITLE not in d.media_check("live.mkv", live)[2]


def test_extension_and_doctype_must_agree() -> None:
    assert refused("movie.webm", mkv()) == "not_media"
    assert refused("movie.mkv", webm()) == "not_media"
    assert refused("movie.mkv", mkv(b"matroskb")) == "not_media"
    # A WebM DocType allows only the WebM codecs.
    assert refused("movie.webm", mkv(b"webm")) == "not_media"
    vp9 = mkv(b"webm", entries=mkv_video_entry(codec=b"V_VP9") + mkv_audio_entry(codec=b"A_OPUS"))
    assert d.media_check("movie.webm", vp9)[0] == "video"


SUBTITLE = el(0xAE, el(0xD7, b"\x03") + el(0x83, b"\x11") + el(0x86, b"S_TEXT/UTF8") + el(0x63A2, b"hello secret"))


@pytest.mark.parametrize(
    "data",
    [
        # An Attachments element: whole files inside the video.
        mkv(extra=el(0x1941A469, el(0x61A7, el(0x466E, b"secret.env") + el(0x465C, SECRET)))),
        # Subtitle, button, complex and metadata tracks.
        mkv(entries=mkv_video_entry() + SUBTITLE),
        mkv(entries=mkv_video_entry() + mkv_video_entry(codec=b"B_VOBBTN", track_type=0x12)),
        mkv(entries=mkv_video_entry() + mkv_video_entry(codec=b"V_MPEG4/ISO/AVC", track_type=0x03)),
        mkv(entries=mkv_video_entry() + mkv_video_entry(codec=b"M_DATA", track_type=0x21)),
        # Codecs outside the allowlist, or of the other kind.
        mkv(entries=mkv_video_entry(codec=b"V_MS/VFW/FOURCC")),
        mkv(entries=mkv_video_entry() + mkv_audio_entry(codec=b"A_MS/ACM")),
        mkv(entries=mkv_video_entry(codec=b"A_AAC")),
        mkv(entries=mkv_video_entry() + mkv_audio_entry(codec=b"V_VP9")),
        # No codec id, two track types.
        mkv(entries=el(0xAE, el(0xD7, b"\x01") + el(0x83, b"\x01"))),
        mkv(entries=mkv_video_entry(el(0x83, b"\x01"))),
        # Audio only is not a video.
        mkv(entries=mkv_audio_entry()),
        # Unknown elements in a walked master.
        mkv(info=el(0x7E8A, SECRET)),
        mkv(entries=mkv_video_entry(el(0x6D80, el(0x6240, SECRET)))),
        mkv(entries=mkv_video_entry(el(0xE0, el(0x7670, el(0x7672, SECRET))))),
        mkv(entries=mkv_video_entry(el(0x7446, b"\x01"))),
        # Lengths: numbers longer than 8 bytes, floats of 5, a seek id of 5, private data over 64 KiB.
        mkv(info=el(0x2AD7B1, SECRET[:9])),
        mkv(info=el(0x4489, SECRET[:5])),
        mkv(extra=el(0x114D9B74, el(0x4DBB, el(0x53AB, b"\x15\x49\xa9\x66\x00")))),
        mkv(entries=mkv_video_entry(el(0x63A2, bytes(64 * 1024 + 1)))),
        mkv(cluster=el(0xE7, SECRET[:9])),
        mkv(extra=el(0x1C53BB6B, el(0xBB, el(0xB3, SECRET[:9])))),
        # Unknown top-level and cluster elements.
        mkv(extra=el(0x1B538667, SECRET)),
        mkv(cluster=el(0xE7, b"\x00") + el(0xEE, SECRET)),
        # Data after the Segment, and a Segment longer than the file.
        mkv() + HTML,
        mkv() + ZIP,
        mkv()[:-1],
    ],
)
def test_mkv_refusals(data: bytes) -> None:
    assert refused("movie.mkv", data) == "not_media"


def test_mkv_size_fields_past_the_end_are_refused() -> None:
    data = bytearray(mkv())
    # The Tracks element claims more bytes than its Segment holds.
    tracks = data.index(b"\x16\x54\xae\x6b")
    data[tracks + 4 : tracks + 12] = b"\x01" + (len(data)).to_bytes(7, "big")
    assert refused("movie.mkv", bytes(data)) == "not_media"


def test_mkv_tags_chapters_and_long_languages_are_blanked() -> None:
    chapters = el(0x1043A770, el(0x45B9, el(0xB6, el(0x80, el(0x85, b"Secret chapter")))))
    long_language = el(0x22B59C, b"en" + SECRET)
    bcp47 = el(0x22B59D, b"en-US")
    data = mkv(extra=chapters, entries=mkv_video_entry(long_language, bcp47) + mkv_audio_entry())
    clean = d.media_check("movie.mkv", data)[2]
    assert b"Secret chapter" not in clean
    assert SECRET not in clean
    assert b"en-US" in clean
    assert len(clean) == len(data)


def test_mkv_crc32_is_recomputed_over_blanked_text() -> None:
    data = mkv(crc=b"\x00\x00\x00\x00")
    clean = d.media_check("movie.mkv", data)[2]
    info = bytes(clean).index(b"\x15\x49\xa9\x66\x01")
    size = int.from_bytes(clean[info + 5 : info + 12], "big")
    crc_value = info + 12 + 9
    covered = clean[crc_value + 4 : info + 12 + size]
    assert clean[crc_value : crc_value + 4] == zlib.crc32(covered).to_bytes(4, "little")
    assert MKV_TITLE not in clean
    assert refused("movie.mkv", mkv(crc=b"\x00" * 5)) == "not_media"
    # One Info and one Tracks element.
    assert refused("movie.mkv", mkv(extra=el(0x1549A966, el(0x2AD7B1, b"\x0f\x42\x40")))) == "not_media"
    assert refused("movie.mkv", mkv(extra=el(0x1654AE6B, mkv_video_entry()))) == "not_media"


# --- Matroska pointers (red-team review): a Seek entry or cue may name only walked elements ------

MKV_HEADER = el(
    0x1A45DFA3,
    el(0x4286, b"\x01")
    + el(0x42F7, b"\x01")
    + el(0x42F2, b"\x04")
    + el(0x42F3, b"\x08")
    + el(0x4282, b"matroska")
    + el(0x4287, b"\x04")
    + el(0x4285, b"\x02"),
)
MKV_INFO = el(0x1549A966, el(0x2AD7B1, b"\x0f\x42\x40") + el(0x4489, struct.pack(">d", 2000.0)))
HIDDEN_ATTACHMENT = el(
    0x1941A469,
    el(
        0x61A7,
        el(0x466E, b"evil.html")
        + el(0x4660, b"text/html")
        + el(0x465C, b"<html><script>alert(1)</script>secret exfil</html>")
        + el(0x46AE, b"\x01"),
    ),
)
HIDDEN_TAGS = el(0x1254C367, el(0x7373, el(0x67C8, el(0x45A3, b"TITLE") + el(0x4487, b"SMUGGLED TEXT IN TAGS"))))
HIDDEN_CHAPTERS = el(
    0x1043A770,
    el(0x45B9, el(0xB6, el(0x73C4, b"\x01") + el(0x91, b"\x00") + el(0x80, el(0x85, b"CHAPTER SMUGGLED")))),
)
HIDDEN_TRACKS = el(
    0x1654AE6B,
    mkv_video_entry()
    + el(0xAE, el(0xD7, b"\x03") + el(0x73C5, b"\x03") + el(0x83, b"\x11") + el(0x86, b"S_TEXT/UTF8")),
)


def hidden_in_a_block(hidden: bytes, hidden_id: int, *, tracks_after: bool = False) -> bytes:
    """An MKV whose only SimpleBlock's frame data is a whole top-level element, with a Seek entry
    pointing at it (players parse whatever they find at a Seek position)."""
    tracks = el(0x1654AE6B, mkv_video_entry() + mkv_audio_entry())
    lead = len(seekhead([(hidden_id, 0)])) + len(MKV_INFO) + (0 if tracks_after else len(tracks))
    timestamp = el(0xE7, b"\x00")
    # Cluster header (4 + 8), the timestamp, the SimpleBlock header (1 + 8) and its 4 block bytes.
    position = lead + 12 + len(timestamp) + 9 + 4
    cluster = el(0x1F43B675, timestamp + el(0xA3, b"\x81\x00\x00\x80" + hidden))
    body = seekhead([(hidden_id, position)]) + MKV_INFO
    body += cluster + tracks if tracks_after else tracks + cluster
    return MKV_HEADER + el(0x18538067, body)


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.mark.parametrize(
    ("hidden", "hidden_id", "tracks_after"),
    [
        (HIDDEN_ATTACHMENT, 0x1941A469, False),
        (HIDDEN_TAGS, 0x1254C367, False),
        (HIDDEN_CHAPTERS, 0x1043A770, False),
        (HIDDEN_TRACKS, 0x1654AE6B, True),
    ],
)
def test_a_seek_entry_into_frame_data_is_refused(hidden: bytes, hidden_id: int, tracks_after: bool) -> None:
    data = hidden_in_a_block(hidden, hidden_id, tracks_after=tracks_after)
    assert refused("movie.mkv", data) == "not_media"
    # The same file with the Seek entry pointing at a real element passes: only the pointer is wrong.
    real = bytearray(data)
    position = real.index(b"\x53\xac\x01") + 10
    real[position : position + 8] = len(seekhead([(0, 0)])).to_bytes(8, "big")
    real[position - 14 : position - 10] = b"\x15\x49\xa9\x66"
    d.media_check("movie.mkv", bytes(real))


@pytest.mark.skipif(FFPROBE is None, reason="ffprobe is not installed")
def test_the_hidden_attachment_is_what_ffprobe_would_have_read(tmp_path: Path) -> None:
    """The attack is real: ffprobe follows the Seek entry into the frame data and finds the file."""
    assert FFPROBE is not None
    path = tmp_path / "att.mkv"
    path.write_bytes(hidden_in_a_block(HIDDEN_ATTACHMENT, 0x1941A469))
    probed = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", str(path)], capture_output=True, text=True, check=False, timeout=60
    )
    assert "evil.html" in probed.stdout


# --- Matroska parser resync (red-team review, round 3) -------------------------------------------

AVCC = b"\x01\x64\x00\x1f\xff\xe1\x00\x04\x67\x64\x00\x1f\x01\x00\x04\x68\xeb\xe3\xcb"
RESYNC_PAD = 0x1000001
"""NUL padding that takes a Language element past libavformat's 16 MiB string limit."""


def resync_mkv(private: bytes, language: bytes) -> bytes:
    """Tracks: an H.264 track with `private` as CodecPrivate, an AAC track, and a second H.264 track
    whose Language is `language`. A Language over 16 MiB makes libavformat fail while it parses
    Tracks; it then resyncs, scanning byte by byte for a top-level id, and lands in CodecPrivate."""

    def video(private: bytes, language: bytes) -> bytes:
        settings = el(0xB0, b"\x07\x80") + el(0xBA, b"\x04\x38")
        return el(
            0xAE,
            el(0xD7, b"\x01")
            + el(0x73C5, bytes(range(1, 9)))
            + el(0x86, b"V_MPEG4/ISO/AVC")
            + el(0x83, b"\x01")
            + el(0x63A2, private)
            + el(0xE0, settings)
            + el(0x22B59C, language),
        )

    tracks = el(0x1654AE6B, video(private, b"und") + mkv_audio_entry() + video(AVCC, language))
    cluster = el(0x1F43B675, el(0xE7, b"\x00") + el(0xA3, b"\x81\x00\x00\x80" + bytes(16)))
    return MKV_HEADER + el(0x18538067, MKV_INFO + tracks + cluster)


def ffprobe_attachments(path: Path) -> list[str]:
    assert FFPROBE is not None
    probed = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return [line for line in probed.stdout.split() if line == "attachment"]


def test_an_attachments_element_in_codec_private_is_refused() -> None:
    padded = b"en" + bytes(RESYNC_PAD)
    assert refused("movie.mkv", resync_mkv(AVCC + HIDDEN_ATTACHMENT, padded)) == "not_media"
    # Without the trigger as well: CodecPrivate is opaque and reachable through any later resync.
    assert refused("movie.mkv", resync_mkv(AVCC + HIDDEN_ATTACHMENT, b"en")) == "not_media"
    # Every top-level id, also the EBML header and the Segment, anywhere in the value.
    for resync_id in d.EBML_RESYNC_IDS:
        assert refused("movie.mkv", resync_mkv(AVCC + b"\x00" + resync_id + b"\x80", b"en")) == "not_media"
    assert d.media_check("movie.mkv", resync_mkv(AVCC, b"en"))[0] == "video"


def test_an_oversized_language_is_blanked_not_kept() -> None:
    """The trigger: a Language over 16 MiB (a valid tag and NULs) is blanked into a Void, which a
    demuxer skips without a length limit, so nothing kept before the first Cluster fails to parse."""
    clean = bytes(d.media_check("movie.mkv", resync_mkv(AVCC, b"en" + bytes(RESYNC_PAD)))[2])
    # The two short languages (und, eng) stay; the padded one is gone.
    assert clean.count(b"\x22\xb5\x9c") == 2
    # The bound: 32 bytes are kept, 33 are blanked.
    kept = bytes(d.media_check("movie.mkv", resync_mkv(AVCC, b"en" + bytes(30)))[2])
    assert el(0x22B59C, b"en" + bytes(30)) in kept
    blanked = bytes(d.media_check("movie.mkv", resync_mkv(AVCC, b"en" + bytes(31)))[2])
    assert el(0x22B59C, b"en" + bytes(31)) not in blanked


def test_a_top_level_id_before_the_first_cluster_is_refused() -> None:
    """Defense in depth: any opaque run before the first Cluster (here a TrackUID) that holds a
    top-level id is refused, wherever the walk lets bytes through unchanged."""
    for resync_id in (b"\x19\x41\xa4\x69", b"\x12\x54\xc3\x67", b"\x16\x54\xae\x6b"):
        uid = el(0x73C5, resync_id + b"\x00\x00\x00\x01")
        entry = el(0xAE, el(0xD7, b"\x03") + uid + el(0x86, b"A_OPUS") + el(0x83, b"\x02"))
        data = mkv(entries=mkv_video_entry() + entry)
        assert refused("movie.mkv", data) == "not_media"
        # The same track with another UID passes: only the id in the opaque bytes is refused.
        other = el(0x73C5, b"\x01" + resync_id[1:] + b"\x00\x00\x00\x01")
        entry = el(0xAE, el(0xD7, b"\x03") + other + el(0x86, b"A_OPUS") + el(0x83, b"\x02"))
        assert d.media_check("movie.mkv", mkv(entries=mkv_video_entry() + entry))[0] == "video"


@pytest.mark.skipif(FFPROBE is None, reason="ffprobe is not installed")
def test_the_resync_attack_is_real_and_the_cleaned_copy_has_no_attachment(tmp_path: Path) -> None:
    """ffprobe on the attack file (as the walk accepted it before this check) shows the hidden file
    as an attachment stream; the cleaned copy of the trigger alone has none."""
    attack = tmp_path / "attack.mkv"
    attack.write_bytes(resync_mkv(AVCC + HIDDEN_ATTACHMENT, b"en" + bytes(RESYNC_PAD)))
    assert ffprobe_attachments(attack) == ["attachment"]
    clean = tmp_path / "clean.mkv"
    clean.write_bytes(d.media_check("movie.mkv", resync_mkv(AVCC, b"en" + bytes(RESYNC_PAD)))[2])
    assert ffprobe_attachments(clean) == []


def test_seek_entries_must_name_the_element_at_their_offset() -> None:
    data = mkv()
    seek = bytes(data).index(b"\x53\xac\x01")
    # Info's entry, one byte off; then Info's offset named as Tracks; then an id outside the list.
    for patch in (
        (seek + 10 + 7, bytes([data[seek + 10 + 7] + 1])),
        (seek - 4, b"\x16\x54\xae\x6b"),
        (seek - 4, b"\x19\x41\xa4\x69"),
        (seek - 4, b"\x00\x00\x00\xec"),
    ):
        broken = bytearray(data)
        broken[patch[0] : patch[0] + len(patch[1])] = patch[1]
        assert refused("movie.mkv", bytes(broken)) == "not_media"
    # Two positions (a parser keeping the first and one keeping the last would disagree), or none.
    info_at = len(seekhead([(0, 0)] * 4)).to_bytes(8, "big")
    entry = el(0x53AB, b"\x15\x49\xa9\x66") + el(0x53AC, info_at)
    assert d.media_check("movie.mkv", mkv(extra=el(0x114D9B74, el(0x4DBB, entry))))[0] == "video"
    twice = el(0x114D9B74, el(0x4DBB, entry + el(0x53AC, info_at)))
    assert refused("movie.mkv", mkv(extra=twice)) == "not_media"
    assert refused("movie.mkv", mkv(extra=el(0x114D9B74, el(0x4DBB, el(0x53AB, b"\x15\x49\xa9\x66"))))) == ("not_media")


def test_the_seek_entry_of_blanked_tags_is_blanked_too() -> None:
    clean = bytes(d.media_check("movie.mkv", mkv())[2])
    assert b"\x53\xab\x01\x00\x00\x00\x00\x00\x00\x04\x12\x54\xc3\x67" not in clean
    assert b"\x53\xab\x01\x00\x00\x00\x00\x00\x00\x04\x15\x49\xa9\x66" in clean
    assert d.media_check("movie.mkv", clean)[2] == clean


def test_cues_must_name_a_walked_cluster_and_block() -> None:
    data = mkv()
    cue = bytes(data).index(b"\xf1\x01")
    relative = bytes(data).index(b"\xf0\x01")
    for at, delta in ((cue + 9 + 7, 1), (relative + 9 + 7, 1), (relative + 9 + 7, 0x40)):
        broken = bytearray(data)
        broken[at] = (broken[at] + delta) & 0xFF
        assert refused("movie.mkv", bytes(broken)) == "not_media"
    # A codec state (a pointer to anything) is refused; 0 means none.
    relative_element = bytes(data[relative : relative + 17])
    for value, accepted in ((1, False), (0, True)):
        state = data.replace(relative_element, el(0xEA, value.to_bytes(8, "big")), 1)
        assert state != data
        if accepted:
            assert d.media_check("movie.mkv", state)[0] == "video"
        else:
            assert refused("movie.mkv", state) == "not_media"
    # A cue without a cluster position.
    no_cluster = el(0x1C53BB6B, el(0xBB, el(0xB3, b"\x00") + el(0xB7, el(0xF7, b"\x01"))))
    assert refused("movie.mkv", mkv(extra=no_cluster)) == "not_media"


def test_block_groups_are_walked() -> None:
    def group(*children: bytes) -> bytes:
        return mkv(cluster=el(0xE7, b"\x00") + el(0xA0, b"".join(children)))

    block = el(0xA1, b"\x81\x00\x00\x80" + bytes(8))
    accepted = group(block, el(0x9B, b"\x10"), el(0xFB, b"\xff\xf0"), el(0x75A2, b"\x01"))
    assert d.media_check("movie.mkv", accepted)[0] == "video"
    for bad in (
        group(block, el(0x75A1, el(0xA6, el(0xEE, b"\x01") + el(0xA5, SECRET)))),  # BlockAdditions
        group(block, el(0xA4, SECRET)),  # CodecState
        group(block, el(0x8E, SECRET)),  # Slices
        group(el(0x9B, b"\x10")),  # no Block
        group(block, block),  # two
        mkv(cluster=el(0xE7, b"\x00") + el(0xAF, SECRET)),  # EncryptedBlock
        mkv(cluster=el(0xE7, b"\x00") + el(0x5854, el(0x58D7, SECRET[:9]))),  # SilentTrackNumber too long
    ):
        assert refused("movie.mkv", bad) == "not_media"
    assert d.media_check("movie.mkv", mkv(cluster=el(0xE7, b"\x00") + el(0x5854, el(0x58D7, b"\x02"))))


def test_track_type_is_a_number() -> None:
    # 01 00 is 256, not 1 (video): a check that strips NUL bytes would read it as video.
    odd = el(0xAE, el(0xD7, b"\x03") + el(0x73C5, b"\x03") + el(0x83, b"\x01\x00") + el(0x86, b"V_VP8"))
    assert refused("movie.mkv", mkv(entries=mkv_video_entry() + odd)) == "not_media"
    # A number with leading zero bytes is still that number; a codec id may end in NUL bytes.
    padded = el(0xAE, el(0xD7, b"\x01") + el(0x73C5, b"\x01") + el(0x83, b"\x00\x01") + el(0x86, b"V_VP8"))
    assert d.media_check("movie.mkv", mkv(entries=padded + mkv_audio_entry()))[0] == "video"
    nul = mkv_video_entry(codec=b"V_MPEG4/ISO/AVC\x00")
    assert d.media_check("movie.mkv", mkv(entries=nul + mkv_audio_entry()))[0] == "video"


def with_header(data: bytes, old: bytes, new: bytes) -> bytes:
    """`data` with the EBML header child `old` replaced by `new` (the header size fixed up)."""
    patched = data.replace(old, new, 1)
    size = int.from_bytes(patched[5:12], "big") + len(new) - len(old)
    return patched[:5] + size.to_bytes(7, "big") + patched[12:]


def test_ebml_header_children_appear_once_with_bounded_lengths() -> None:
    data = mkv()
    doctype = el(0x4282, b"matroska")
    # Two DocTypes (a parser that keeps the last one sees webm), in either order.
    assert refused("movie.mkv", with_header(data, doctype, doctype + el(0x4282, b"webm"))) == "not_media"
    assert refused("movie.webm", with_header(data, doctype, el(0x4282, b"webm") + doctype)) == "not_media"
    assert d.sniff_media(with_header(data, doctype, doctype + el(0x4282, b"webm"))) is None
    # A version twice, a number of 9 bytes, a DocType of 17.
    version = el(0x4286, b"\x01")
    assert refused("movie.mkv", with_header(data, version, version + version)) == "not_media"
    assert refused("movie.mkv", with_header(data, version, el(0x4286, b"\x01" + SECRET[:8]))) == "not_media"
    assert refused("movie.mkv", with_header(data, version, el(0x4286, b"\x01" * 7000))) == "not_media"
    assert refused("movie.mkv", with_header(data, doctype, el(0x4282, b"matroska" + bytes(9)))) == "not_media"


def test_nested_crcs_are_recomputed_innermost_first() -> None:
    entry = mkv_video_entry()
    inner = el(0xAE, el(0xBF, bytes(4)) + entry[9:])
    tracks_payload = el(0xBF, bytes(4)) + inner + mkv_audio_entry()
    data = mkv(entries=tracks_payload)
    clean = bytes(d.media_check("movie.mkv", data)[2])
    assert MKV_TRACK_NAME not in clean
    tracks = clean.index(b"\x16\x54\xae\x6b\x01")
    payload = tracks + 12
    end = payload + int.from_bytes(clean[tracks + 5 : tracks + 12], "big")
    outer = payload + 9
    assert clean[outer : outer + 4] == zlib.crc32(clean[outer + 4 : end]).to_bytes(4, "little")
    entry_at = outer + 4
    entry_payload = entry_at + 9
    entry_end = entry_payload + int.from_bytes(clean[entry_at + 2 : entry_at + 9], "big")
    value = entry_payload + 9
    assert clean[value : value + 4] == zlib.crc32(clean[value + 4 : entry_end]).to_bytes(4, "little")


@pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg is not installed")
def test_a_long_real_recording_keeps_working_seek_pointers(tmp_path: Path) -> None:
    """Many clusters and cues with relative positions (ffmpeg writes both), Opus in BlockGroups
    with DiscardPadding: accepted, and the cleaned copy shows no chapters, tags or extra streams."""
    assert FFMPEG is not None
    assert FFPROBE is not None
    source = tmp_path / "long.mkv"
    lavfi = ["-f", "lavfi", "-i", "testsrc=size=64x48:rate=10", "-f", "lavfi", "-i", "sine=sample_rate=48000"]
    command = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", *lavfi, "-t", "12"]
    command += ["-c:v", "libx264", "-g", "10", "-c:a", "libopus", "-cluster_time_limit", "500", str(source)]
    if subprocess.run(command, capture_output=True, check=False, timeout=60).returncode != 0:
        pytest.skip("this ffmpeg cannot write H.264 and Opus")
    _, _, clean = d.media_check("long.mkv", source.read_bytes())
    cleaned = tmp_path / "clean.mkv"
    cleaned.write_bytes(clean)
    probed = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_chapters", "-show_format", str(cleaned)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert probed.returncode == 0, probed.stderr
    assert probed.stdout.count("[STREAM]") == 2
    assert "[CHAPTER]" not in probed.stdout
    assert "TAG:" not in probed.stdout.replace("TAG:language=", "")
    # Seeking to the middle uses the cues.
    seek = subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-xerror", "-ss", "6", "-i", str(cleaned), "-f", "null", "-"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert seek.returncode == 0, seek.stderr


def test_the_element_budget_grows_with_the_file() -> None:
    assert d._Budget().left == d.MAX_MEDIA_ELEMENTS
    assert d._Budget(250_000_000).left == d.MAX_MEDIA_ELEMENTS + 250_000_000 // 1024


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
@pytest.mark.parametrize(
    ("name", "codecs"),
    [
        ("obs.mkv", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
        ("flac.mkv", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "flac"]),
        ("clip.webm", ["-c:v", "libvpx-vp9", "-c:a", "libopus"]),
    ],
)
def test_a_real_recording_passes_and_still_plays(tmp_path: Path, name: str, codecs: list[str]) -> None:
    """A real recording made by ffmpeg (the muxer OBS uses for MKV): accepted, its title and the
    muxing application gone, and the cleaned copy still decodes from start to end."""
    assert FFMPEG is not None
    source = tmp_path / name
    lavfi = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=30", "-f", "lavfi", "-i", "sine=sample_rate=48000"]
    command = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", *lavfi, "-t", "1", *codecs]
    command += ["-metadata", "title=Secret title", "-metadata:s:v:0", "title=Secret camera", str(source)]
    made = subprocess.run(command, capture_output=True, check=False, timeout=60)
    if made.returncode != 0:
        pytest.skip(f"this ffmpeg cannot write {name}")
    data = source.read_bytes()
    assert b"Secret title" in data
    _, _, clean = d.media_check(name, data)
    assert b"Secret" not in clean
    assert b"Lavf" not in clean
    cleaned = tmp_path / f"clean-{name}"
    cleaned.write_bytes(clean)
    decoded = subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-xerror", "-i", str(cleaned), "-f", "null", "-"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert decoded.returncode == 0, decoded.stderr
    assert decoded.stderr == b""
