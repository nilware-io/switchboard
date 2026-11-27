"""`dispatcher.media_check`: only whole image, audio and video files pass, decided by magic bytes
and a walk of the whole container, with metadata stripped where that is cheap and safe."""

from __future__ import annotations

import struct
import time
import zlib

import pytest

from tests.remote.media_fixtures import (
    GPS,
    HTML,
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
    exif_segment,
    flac,
    flac_block,
    gif,
    gif_comment,
    hdlr,
    jpeg,
    jpeg_segment,
    mp3,
    mp4,
    ogg,
    png,
    png_chunk,
    png_sized,
    riff_chunk,
    trak,
    wav,
    webm,
    webp,
    zip_of_secret,
    zip_secret,
)
from voice_coding_mcp.remote import dispatcher as d


def refused(name: str, data: bytes) -> str:
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
    with_exif = webp(riff_chunk(b"EXIF", b"MM\x00\x2a" + GPS), riff_chunk(b"XMP ", GPS), flags=0x0C)
    _, _, clean = d.media_check("pic.webp", with_exif)
    assert GPS not in clean
    assert clean == webp(flags=0)


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
    video = mp4(mdat=bytes(d.ATTACHMENT_CAPS["video"]))
    assert refused("clip.mp4", video) == "too_large"
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
    icc = jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + bytes(16))
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
    tracks = ebml_short(b"\x16\x54\xae\x6b", ebml_short(b"\xae", ebml_short(b"\xd7", b"\x01")))
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


def test_millions_of_tiny_elements_are_refused_fast() -> None:
    doctype = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x82", b"webm"))
    tracks = ebml_short(b"\x16\x54\xae\x6b", ebml_short(b"\xae", b""))
    voids = b"\xec\x80" * ((d.ATTACHMENT_CAPS["video"] - 100) // 2)
    cases = {
        "x.webm": doctype + b"\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff" + tracks + voids,
        "x.mp4": FTYP + MOOV + MDAT + box(b"free", b"") * ((d.ATTACHMENT_CAPS["video"] - 1000) // 8),
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
