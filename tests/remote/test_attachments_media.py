"""`dispatcher.media_check`: only whole image, audio and video files pass, decided by magic bytes
and a walk of the whole container, with metadata stripped where that is cheap and safe."""

from __future__ import annotations

import struct

import pytest

from tests.remote.media_fixtures import (
    GPS,
    HTML,
    NOT_MEDIA,
    SVG,
    VALID,
    ZIP,
    crc_bitwise,
    exif_segment,
    flac,
    flac_block,
    gif,
    gif_comment,
    jpeg,
    jpeg_segment,
    mp3,
    mp4,
    ogg,
    png,
    png_chunk,
    riff_chunk,
    wav,
    webm,
    webp,
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


def test_ogg_and_webm_tags_are_left_as_they_are() -> None:
    # Documented: Vorbis and Opus comments and Matroska tags are not stripped.
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
