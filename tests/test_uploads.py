# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Reading an upload's body and preparing its images, before any write.

Every image here is synthetic, made by the test. The body reader is driven with byte chunks
as a request's stream gives them, and prepared images go into a draft store on a temporary
household file.
"""

import asyncio
import hashlib
import io
import itertools
import logging
import sqlite3
import struct
import tempfile
import threading
import time
import types
import warnings
import zlib
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import ExifTags, Image, PngImagePlugin

from blossom import uploads
from blossom.app import create_app
from blossom.stores import import_drafts
from blossom.stores.gradebook import ADD_ACCEPTANCE
from blossom.stores.import_drafts import (
    MOST_IMAGES_PRESENT,
    DraftClosed,
    DraftRow,
    ImportDraftsStore,
    NewImage,
    ReadTiming,
    StoredReading,
    TooManyImages,
    UploadedReading,
    new_creation_token,
)
from blossom.stores.project_state import ProjectStateStore
from blossom.uploads import (
    LONG_EDGE,
    MEGABYTE,
    MOST_FILES,
    MOST_IMAGE_BYTES,
    MOST_PIXELS,
    MOST_UPLOAD_BYTES,
    PROVIDER_IMAGE_LIMIT,
    ImageRefused,
    UploadRefused,
    base64_length,
    prepare_image,
    prepare_upload,
    read_upload,
)
from tests.support import OBSERVED_AT, SAME_ORIGIN, fixture_clock, fixture_settings

BOUNDARY = "synthetic-boundary-7f3a"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"
FIELDS = {"creation_token": 64, "page_version": 16}
FILE_FIELD = "screenshots"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

Part = tuple[str, str | None, bytes]
"""A part's field name, its file name (None for a text field), and its bytes."""


def body_of(parts: Iterable[Part]) -> bytes:
    pieces = []
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        head = f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
        if filename is not None:
            head += "Content-Type: image/png\r\n"
        pieces.append(head.encode() + b"\r\n" + data + b"\r\n")
    pieces.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(pieces)


class Stream:
    """A request body in chunks, counting what the reader took."""

    def __init__(self, body: bytes, chunk: int = 64 * 1024) -> None:
        self.body = body
        self.chunk = chunk
        self.taken = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.body), self.chunk):
            piece = self.body[start : start + self.chunk]
            self.taken += len(piece)
            yield piece


def read(
    body: bytes,
    *,
    content_type: str | None = CONTENT_TYPE,
    length: str | None = "declared",
    stream: Stream | None = None,
) -> uploads.Upload:
    source = stream or Stream(body)
    declared = str(len(body)) if length == "declared" else length
    return asyncio.run(
        read_upload(content_type, declared, aiter(source), fields=FIELDS, file_field=FILE_FIELD)
    )


def refusal(body: bytes, **options: object) -> str:
    with pytest.raises(UploadRefused) as refused:
        read(body, **options)  # type: ignore[arg-type]
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None
    return refused.value.kind


def png(width: int = 64, height: int = 48, color: tuple[int, ...] = (200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg(image: Image.Image, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, **options)
    return buffer.getvalue()


def chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png_header(width: int, height: int) -> bytes:
    """A PNG whose header declares ``width`` x ``height``, with pixel data that isn't real."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", b"not pixel data")
        + chunk(b"IEND", b"")
    )


def jpeg_header(width: int, height: int) -> bytes:
    """A JPEG whose frame segment declares ``width`` x ``height``, with no image data."""
    frame = bytes([8]) + height.to_bytes(2) + width.to_bytes(2)
    frame += bytes([3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1])
    return b"\xff\xd8\xff\xc0" + (len(frame) + 2).to_bytes(2) + frame + b"\xff\xd9"


def chunks_of(data: bytes) -> list[bytes]:
    """Every chunk type in a PNG, in order."""
    kinds, at = [], len(PNG_SIGNATURE)
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        kinds.append(data[at + 4 : at + 8])
        at += 12 + length
    return kinds


def noise(width: int, height: int) -> Image.Image:
    """Pixels that don't compress, the same at every run."""
    blocks = (width * height * 3 + 31) // 32
    data = b"".join(hashlib.sha256(n.to_bytes(4, "big")).digest() for n in range(blocks))
    return Image.frombytes("RGB", (width, height), data[: width * height * 3])


def pixel(data: bytes, at: tuple[int, int]) -> tuple[int, int, int]:
    """One pixel of a PNG, as red, green and blue."""
    value = Image.open(io.BytesIO(data)).convert("RGB").getpixel(at)
    assert isinstance(value, tuple)
    red, green, blue = value[:3]
    return int(red), int(green), int(blue)


# ------------------------------------------------------------------ a household file


def no_rows(readings: Sequence[StoredReading]) -> list[DraftRow]:
    return [DraftRow(key=f"{reading.sha256}:row", readable=True) for reading in readings]


class Household:
    """Her record and a draft store on one temporary file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        record = ProjectStateStore.open(path, fixture_clock())
        self.student = record.student_id()
        record.close()
        self.store = self.another_store()

    def another_store(self) -> ImportDraftsStore:
        return ImportDraftsStore.open(
            self.path, fixture_clock(), rows={"homework_screenshot": no_rows}
        )

    def create(self, files: Sequence[bytes], token: str | None = None) -> str | None:
        prepared = prepare_upload(files)
        made = self.store.create(
            self.student,
            kind="homework_screenshot",
            made_by="parent",
            creation_token=token or new_creation_token(),
            images=[image for _, image in prepared.images],
        )
        return None if made is None else made.draft_id

    def accept(self, draft_id: str, *keys: str) -> None:
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(
                ADD_ACCEPTANCE,
                (
                    f"acceptance-{draft_id}-{len(keys)}",
                    self.student,
                    "homework_screenshot",
                    draft_id,
                    None,
                    None,
                    None,
                    "[" + ",".join(f'["{key}", "assignment"]' for key in keys) + "]",
                    "matches",
                    "shown",
                    None,
                    len(keys),
                    0,
                    0,
                    0,
                    len(keys),
                    0,
                    1,
                    OBSERVED_AT.isoformat(),
                    "parent",
                ),
            )
        connection.close()


@pytest.fixture
def household(tmp_path: Path) -> Household:
    return Household(tmp_path / "blossom.sqlite3")


# ------------------------------------------------------------------ the body's limits


def test_a_declared_length_past_the_limit_is_refused_before_any_byte_is_read() -> None:
    stream = Stream(body_of([(FILE_FIELD, "a.png", png())]))

    kind = refusal(b"", length=str(MOST_UPLOAD_BYTES + 1), stream=stream)

    assert kind == "body_too_large"
    assert stream.taken == 0


def test_a_body_without_a_length_is_refused_within_one_chunk_past_the_limit() -> None:
    piece = b"\x00" * (11 * MEGABYTE)
    body = body_of([(FILE_FIELD, f"{n}.png", piece) for n in range(4)])
    stream = Stream(body, chunk=MEGABYTE)

    kind = refusal(body, length=None, stream=stream)

    assert kind == "body_too_large"
    assert MOST_UPLOAD_BYTES < stream.taken <= MOST_UPLOAD_BYTES + MEGABYTE


def test_an_eleventh_file_part_is_refused() -> None:
    body = body_of([(FILE_FIELD, f"{n}.png", b"x") for n in range(MOST_FILES + 1)])

    assert refusal(body) == "too_many_files"


def test_a_file_part_past_its_limit_is_refused() -> None:
    body = body_of([(FILE_FIELD, "big.png", b"\x00" * (MOST_IMAGE_BYTES + 1))])

    assert refusal(body) == "file_too_large"


@pytest.mark.parametrize(
    ("parts", "kind"),
    [
        ([("surprise", None, b"x")], "unknown_field"),
        ([("creation_token", None, b"a"), ("creation_token", None, b"b")], "repeated_field"),
        ([("page_version", None, b"9" * 17)], "field_too_large"),
        ([(FILE_FIELD, "", b"")] * (MOST_FILES + len(FIELDS) + 1), "too_many_parts"),
        ([("page_version", None, b"\xff\xfe")], "unreadable"),
    ],
)
def test_each_field_rule_refuses_the_whole_upload(parts: list[Part], kind: str) -> None:
    assert refusal(body_of(parts)) == kind


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "text/plain",
        "multipart/form-data; boundary=\u2603",
    ],
)
def test_a_body_that_isnt_multipart_with_a_boundary_is_refused(content_type: str | None) -> None:
    assert refusal(b"creation_token=x", content_type=content_type) == "not_multipart"


def test_a_declared_length_in_other_digits_is_unreadable() -> None:
    body = body_of([(FILE_FIELD, "a.png", png())])

    for declared in ("\u00b9", "\u0661\u0662", "12 ", "-1"):
        with pytest.raises(UploadRefused) as refused:
            read(body, length=declared)
        assert refused.value.kind == "unreadable"
        assert declared not in str(refused.value)
        assert refused.value.__context__ is None


def test_a_cut_off_or_malformed_body_is_unreadable() -> None:
    whole = body_of([(FILE_FIELD, "a.png", png())])

    assert refusal(whole[: len(whole) // 2]) == "unreadable"
    assert refusal(b"--" + BOUNDARY.encode() + b"\r\nno colon here\r\n\r\n") == "unreadable"


def test_an_empty_file_part_is_no_file_and_the_fields_are_read_once() -> None:
    body = body_of(
        [
            ("creation_token", None, b"0123abcd"),
            (FILE_FIELD, "", b""),
            ("page_version", None, b"3"),
        ]
    )

    upload = read(body)

    assert upload.files == ()
    assert upload.fields == {"creation_token": "0123abcd", "page_version": "3"}


def test_files_come_back_in_the_order_they_were_sent() -> None:
    first, second = png(color=(1, 2, 3)), png(color=(4, 5, 6))

    upload = read(body_of([(FILE_FIELD, "1.png", first), (FILE_FIELD, "2.png", second)]))

    assert upload.files == (first, second)


def test_a_first_upload_with_no_valid_image_makes_no_draft(household: Household) -> None:
    upload = read(body_of([(FILE_FIELD, "notes.png", b"plain text, not an image")]))

    assert household.create(upload.files) is None
    connection = sqlite3.connect(household.path)
    assert connection.execute("SELECT count(*) FROM import_drafts").fetchone()[0] == 0
    connection.close()


# ------------------------------------------------------------------ what an image may be


def other_kinds() -> list[bytes]:
    gif, webp = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(gif, format="GIF")
    Image.new("RGB", (8, 8), (1, 2, 3)).save(webp, format="WEBP")
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    pdf = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
    return [gif.getvalue(), webp.getvalue(), heic, pdf, b"named .png, sent as image/png"]


def test_other_kinds_are_refused_by_content_and_named_by_place(household: Household) -> None:
    files = [png(color=(9, 9, 9)), *other_kinds(), png(color=(8, 8, 8))]

    prepared = prepare_upload(files)

    assert [place for place, _ in prepared.images] == [0, 6]
    assert prepared.refused == (
        (1, "not_png_or_jpeg"),
        (2, "not_png_or_jpeg"),
        (3, "heic"),
        (4, "not_png_or_jpeg"),
        (5, "not_png_or_jpeg"),
    )
    draft_id = household.create(files)
    assert draft_id is not None
    assert len(household.store.snapshot(household.student, draft_id).images) == 2


def test_a_header_past_the_pixel_limit_is_refused_before_pixel_data_is_decoded() -> None:
    assert MOST_PIXELS == 30_000_000
    assert Image.MAX_IMAGE_PIXELS == MOST_PIXELS

    with pytest.raises(ImageRefused) as refused:
        prepare_image(png_header(MOST_PIXELS + 1, 1))
    assert refused.value.kind == "too_many_pixels"
    with pytest.raises(ImageRefused) as bomb:
        prepare_image(png_header(20_000, 20_000))
    assert bomb.value.kind == "too_many_pixels"


def test_a_header_past_the_pixel_limit_is_refused_before_the_image_library_warns() -> None:
    headers = [png_header(MOST_PIXELS + 1, 1), png_header(8064, 6048), jpeg_header(8064, 6048)]

    kinds = []

    with warnings.catch_warnings(record=True) as seen:
        for header in headers:
            with pytest.raises(ImageRefused) as refused:
                prepare_image(header)
            kinds.append(refused.value.kind)

    assert [str(warning.message) for warning in seen] == []
    assert kinds == ["too_many_pixels"] * 3


def test_the_declared_size_is_read_from_a_real_png_and_jpeg() -> None:
    exif = Image.Exif()
    exif[ExifTags.Base.Artist] = "Wren"
    photo = jpeg(Image.new("RGB", (70, 30), (1, 2, 3)), exif=exif.tobytes(), comment=b"x")

    assert uploads.declared_size(png(64, 48)) == (64, 48)
    assert uploads.declared_size(photo) == (70, 30)
    assert uploads.declared_size(b"\xff\xd8\xff\xe0") is None


def test_a_sixteen_bit_gray_png_keeps_its_shade() -> None:
    buffer = io.BytesIO()
    Image.new("I;16", (8, 8), 32768).save(buffer, format="PNG")
    assert Image.open(io.BytesIO(buffer.getvalue())).mode.startswith("I")

    prepared = prepare_image(buffer.getvalue())

    red, green, blue = pixel(prepared.png, (4, 4))
    assert red == green == blue
    assert 120 <= red <= 135


def test_a_sixteen_bit_gray_transparency_color_hides_exactly_its_own_pixels() -> None:
    shades = [0, 32768, 32700, 65535, 32768, 16384, 32896, 32768]
    wide = Image.new("I;16", (len(shades), 1))
    wide.putdata(shades)
    buffer = io.BytesIO()
    wide.save(buffer, format="PNG", transparency=32768)
    assert Image.open(io.BytesIO(buffer.getvalue())).info["transparency"] == 32768

    prepared = Image.open(io.BytesIO(prepare_image(buffer.getvalue()).png))

    assert prepared.mode == "RGBA"
    alpha = [prepared.getpixel((x, 0))[3] for x in range(len(shades))]  # type: ignore[index]
    assert alpha == [0 if shade == 32768 else 255 for shade in shades]
    assert prepared.getpixel((3, 0))[:3] == (255, 255, 255)  # type: ignore[index]
    assert prepared.getpixel((0, 0))[:3] == (0, 0, 0)  # type: ignore[index]


def test_broken_and_animated_images_are_refused_each_alone() -> None:
    whole = jpeg(Image.new("RGB", (64, 64), (10, 200, 10)))
    good = png()
    broken = bytearray(good)
    broken[29] ^= 0xFF
    animated = io.BytesIO()
    frames = [Image.new("RGB", (16, 16), color) for color in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(animated, format="PNG", save_all=True, append_images=frames[1:])

    prepared = prepare_upload([whole[: len(whole) // 2], bytes(broken), animated.getvalue(), good])

    assert prepared.refused == ((0, "undecodable"), (1, "undecodable"), (2, "several_frames"))
    assert [place for place, _ in prepared.images] == [3]


def test_a_jpeg_with_a_second_frame_is_read_from_its_first() -> None:
    first = Image.new("RGB", (32, 32), (250, 10, 10))
    second = Image.new("RGB", (32, 32), (10, 10, 250))
    buffer = io.BytesIO()
    first.save(buffer, format="MPO", save_all=True, append_images=[second], quality=95)

    prepared = prepare_image(buffer.getvalue())

    red, _, blue = pixel(prepared.png, (16, 16))
    assert red > 200
    assert blue < 50


def test_orientation_is_applied_and_no_metadata_or_sentinel_is_kept() -> None:
    sideways = Image.new("RGB", (40, 20), (250, 10, 10))
    sideways.paste((10, 10, 250), (20, 0, 40, 20))
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSAreaInformation] = "Sentinel-Gps-L5"
    photo = jpeg(sideways, exif=exif.tobytes(), comment=b"Sentinel-Comment-J1")
    text = PngImagePlugin.PngInfo()
    text.add_text("Comment", "Sentinel-Text-T2")
    noted = io.BytesIO()
    Image.new("RGBA", (30, 30), (1, 2, 3, 128)).save(noted, format="PNG", pnginfo=text)
    assert b"Sentinel-Gps-L5" in photo
    assert b"Sentinel-Text-T2" in noted.getvalue()

    upright = prepare_image(photo)
    clean = prepare_image(noted.getvalue())

    assert (upright.width, upright.height) == (20, 40)
    assert pixel(upright.png, (10, 5))[0] > 200
    assert pixel(upright.png, (10, 35))[2] > 200
    for prepared in (upright, clean):
        assert chunks_of(prepared.png) == [b"IHDR", b"IDAT", b"IEND"]
        assert b"Sentinel" not in prepared.png
    assert Image.open(io.BytesIO(clean.png)).mode == "RGBA"
    assert Image.open(io.BytesIO(upright.png)).mode == "RGB"


def test_a_long_edge_past_the_limit_is_scaled_and_a_smaller_one_kept(
    household: Household,
) -> None:
    wide = png(3000, 1500)
    small = png(800, 600)

    scaled, kept = prepare_image(wide), prepare_image(small)

    assert (scaled.width, scaled.height) == (LONG_EDGE, 784)
    assert Image.open(io.BytesIO(scaled.png)).size == (LONG_EDGE, 784)
    assert (kept.width, kept.height) == (800, 600)
    draft_id = household.create([wide])
    assert draft_id is not None
    stored = household.store.snapshot(household.student, draft_id).images[0]
    assert stored.sha256 == hashlib.sha256(scaled.png).hexdigest()


def test_an_image_past_the_providers_size_is_stepped_down_until_it_fits(
    household: Household,
) -> None:
    source = io.BytesIO()
    noise(400, 300).save(source, format="PNG")
    limit = 100_000

    prepared = prepare_image(source.getvalue(), limit=limit)

    sizes = [(width, height) for width, height, _ in prepared.steps]
    lengths = [length for _, _, length in prepared.steps]
    assert len(sizes) > 2
    assert all(a[0] > b[0] and a[1] > b[1] for a, b in itertools.pairwise(sizes))
    assert all(length > limit for length in lengths[:-1])
    assert lengths[-1] <= limit
    assert lengths[-1] == base64_length(prepared.png)
    assert sizes[-1] == (prepared.width, prepared.height)
    assert prepare_image(source.getvalue(), limit=limit).png == prepared.png
    made = household.store.create(
        household.student,
        kind="homework_screenshot",
        made_by="parent",
        creation_token=new_creation_token(),
        images=[uploads.as_new_image(prepared)],
    )
    assert made is not None
    shown = household.store.image_png(household.student, made.draft_id, made.added[0])
    assert shown == prepared.png


def test_the_recorded_provider_limit_is_larger_than_a_one_pixel_png() -> None:
    one_pixel = io.BytesIO()
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(one_pixel, format="PNG")

    assert PROVIDER_IMAGE_LIMIT == 10_000_000
    assert base64_length(one_pixel.getvalue()) < 100 < PROVIDER_IMAGE_LIMIT


def test_a_screenshot_sized_image_fits_at_the_first_try() -> None:
    assert len(prepare_image(png(1568, 1100)).steps) == 1


# ------------------------------------------------------------------ no temporary file


def test_an_upload_writes_no_temporary_file(
    household: Household, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "temporary"
    empty.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(empty))
    monkeypatch.setenv("TMPDIR", str(empty))

    def no_temporary_file(*args: object, **kwargs: object) -> object:
        msg = "a temporary file was asked for"
        raise AssertionError(msg)

    for name in (
        "mkstemp",
        "mkdtemp",
        "TemporaryFile",
        "NamedTemporaryFile",
        "SpooledTemporaryFile",
        "TemporaryDirectory",
    ):
        monkeypatch.setattr(tempfile, name, no_temporary_file)
    body = body_of(
        [
            ("creation_token", None, b"abc"),
            (FILE_FIELD, "a.png", png(2000, 1200)),
            (FILE_FIELD, "b.jpg", jpeg(Image.new("RGB", (900, 700), (5, 5, 5)))),
        ]
    )

    upload = read(body, stream=Stream(body, chunk=256 * 1024))
    draft_id = household.create(upload.files)

    assert draft_id is not None
    assert len(household.store.snapshot(household.student, draft_id).images) == 2
    assert list(empty.iterdir()) == []


# ------------------------------------------------------------------ the store's side of an upload


def test_the_same_first_upload_sent_twice_makes_one_draft(household: Household) -> None:
    token = new_creation_token()
    files = read(body_of([(FILE_FIELD, "a.png", png())])).files

    first = household.create(files, token)
    again = household.store.create(
        household.student,
        kind="homework_screenshot",
        made_by="parent",
        creation_token=token,
        images=[image for _, image in prepare_upload(files).images],
    )

    assert again is not None
    assert again.draft_id == first
    assert (again.added, again.duplicates) == ((), (0,))


@pytest.mark.parametrize("first", ["save", "upload"])
def test_an_upload_never_commits_into_a_finished_draft(household: Household, first: str) -> None:
    draft_id = household.create([png(color=(1, 1, 1))])
    assert draft_id is not None
    snapshot = household.store.snapshot(household.student, draft_id)
    image_id = snapshot.images[0].image_id
    attempt = household.store.start_attempt(household.student, draft_id, [image_id])
    assert attempt is not None
    household.store.store_reading(
        household.student,
        draft_id,
        attempt_id=attempt.attempt_id,
        image_id=image_id,
        sha256=snapshot.images[0].sha256,
        reading=UploadedReading("homework_list", "v1b", "screenshot-reading/1", "{}"),
        timing=ReadTiming(1.0, 1, 1),
    )
    key = f"{snapshot.images[0].sha256}:row"
    later = [image for _, image in prepare_upload([png(color=(2, 2, 2))]).images]

    if first == "save":
        household.accept(draft_id, key)
        with pytest.raises(DraftClosed):
            household.store.add_images(household.student, draft_id, later)
        assert len(household.store.snapshot(household.student, draft_id).images) == 1
    else:
        household.store.add_images(household.student, draft_id, later)
        household.accept(draft_id, key)
        after = household.store.snapshot(household.student, draft_id)
        assert (after.finished, after.closed_at, len(after.images)) == (False, None, 2)


def test_decodes_run_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    inside, most = 0, 0
    counting = threading.Lock()
    real = uploads._prepared

    def slowed(data: bytes, limit: int) -> uploads.Prepared:
        nonlocal inside, most
        with counting:
            inside += 1
            most = max(most, inside)
        time.sleep(0.05)
        try:
            return real(data, limit)
        finally:
            with counting:
                inside -= 1

    monkeypatch.setattr(uploads, "_prepared", slowed)
    workers = [
        threading.Thread(target=prepare_image, args=(png(color=(n, n, n)),)) for n in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert most == 1


def test_the_store_hashes_an_upload_before_it_takes_the_writer(
    household: Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another connection can take the file's writer while the store hashes, so the store's
    write transaction holds only the inserts."""
    free: list[bool] = []
    real = hashlib.sha256

    def probing(data: bytes) -> "hashlib._Hash":
        other = sqlite3.connect(household.path, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            free.append(True)
        except sqlite3.OperationalError:
            free.append(False)
        finally:
            other.close()
        return real(data)

    monkeypatch.setattr(import_drafts, "hashlib", types.SimpleNamespace(sha256=probing))
    images = [image for _, image in prepare_upload([png(color=(3, 3, 3))]).images]

    made = household.store.create(
        household.student,
        kind="homework_screenshot",
        made_by="parent",
        creation_token=new_creation_token(),
        images=images,
    )

    assert made is not None
    assert free == [True]


def test_two_uploads_racing_for_the_last_place_commit_one(household: Household) -> None:
    draft_id = household.create([png(color=(n, 0, 0)) for n in range(MOST_IMAGES_PRESENT - 1)])
    assert draft_id is not None
    stores = [household.store, household.another_store()]
    images = [[image for _, image in prepare_upload([png(color=(0, n, 0))]).images] for n in (1, 2)]
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def race(store: ImportDraftsStore, batch: list[NewImage]) -> None:
        start.wait()
        try:
            store.add_images(household.student, draft_id, batch)
            outcomes.append("committed")
        except TooManyImages:
            outcomes.append("refused")

    racers = [threading.Thread(target=race, args=pair) for pair in zip(stores, images, strict=True)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()
    stores[1].close()

    assert sorted(outcomes) == ["committed", "refused"]
    assert len(household.store.snapshot(household.student, draft_id).images) == MOST_IMAGES_PRESENT


# ------------------------------------------------------------------ nothing reaches a log


def test_the_image_library_logger_is_held_above_debug_from_startup() -> None:
    logging.getLogger("PIL").setLevel(logging.NOTSET)

    with TestClient(create_app(fixture_settings()), headers=SAME_ORIGIN):
        held = logging.getLogger("PIL").level

    assert held > logging.DEBUG


def test_no_sentinel_from_an_image_or_a_body_reaches_a_log_or_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every logger set to DEBUG before the app starts, then the app's own holds, as a
    household that turned its logging up would have them."""
    loggers = [logging.getLogger(name) for name in ("PIL", "python_multipart")]
    levels = [logger.level for logger in loggers]
    # The synthetic files are made with Pillow's lines held, so only the app's own reads of
    # them could put a sentinel in the log.
    loggers[0].setLevel(logging.INFO)
    exif = Image.Exif()
    exif[ExifTags.Base.Artist] = "Sentinel-Artist-W9"
    exif[ExifTags.Base.Orientation] = 3
    signed = jpeg(Image.new("RGB", (16, 16), (90, 90, 90)), exif=exif.tobytes())
    text = PngImagePlugin.PngInfo()
    text.add_text("Comment", "Sentinel-Text-T2")
    noted = io.BytesIO()
    Image.frombytes("RGB", (4, 4), b"Sentinel-Pixel-P3" + b"\x00" * 31).save(
        noted, format="PNG", pnginfo=text
    )
    broken = bytearray(noted.getvalue())
    broken[-20] ^= 0xFF
    errors: list[BaseException] = []
    try:
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        with (
            TestClient(create_app(fixture_settings()), headers=SAME_ORIGIN),
            caplog.at_level(logging.DEBUG),
        ):
            prepare_image(signed)
            prepare_image(noted.getvalue())
            for data in (bytes(broken), b"Sentinel-Text-T2 is not an image"):
                with pytest.raises(ImageRefused) as refused:
                    prepare_image(data)
                errors.append(refused.value)
            for body in (
                body_of([("page_version", None, b"Sentinel-\xff-U4")]),
                b"--" + BOUNDARY.encode() + b"\r\nSentinel-Header-H5\r\n\r\n",
            ):
                with pytest.raises(UploadRefused) as refused_body:
                    read(body)
                errors.append(refused_body.value)
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)

    assert "Sentinel" not in caplog.text
    for error in errors:
        assert "Sentinel" not in str(error)
        assert error.__context__ is None
        assert error.__cause__ is None
