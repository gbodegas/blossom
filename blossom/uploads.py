# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Reading an upload's body and preparing its images, all in memory and before any write.

The body is read from the request's stream by ``python_multipart.MultipartParser`` with
callbacks alone, so no part ever reaches a temporary file. Every limit is checked as bytes
arrive: the whole body, the number of parts, each file, and each named text field. Any of
them refuses the whole upload, by its kind and nothing it read.

Each image is then prepared: opened only as a PNG or a JPEG, its pixel count checked from the
header before any pixel is decoded, a JPEG turned upright and read from its first frame, and
re-encoded as an 8-bit PNG with no metadata, at most 1,568 px on its long edge. When that PNG
is over the provider's size for one image, the same pixels are prepared again at strictly
smaller dimensions until it fits. The prepared PNG is the one stored, sent and shown.

An error that can carry what it read (the parser's, Pillow's, a UTF-8 decode) is turned into
a closed kind outside its ``except`` block, so nothing keeps a reference to it.
"""

import io
import logging
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Final, NamedTuple, NoReturn

from PIL import Image, ImageOps, JpegImagePlugin, PngImagePlugin
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from blossom.stores.import_drafts import NewImage

MEGABYTE: Final = 1_000_000
"""A megabyte in the upload limits: a million bytes, the smaller reading of the word."""

MOST_UPLOAD_BYTES: Final = 40 * MEGABYTE
"""The most one upload's body may hold."""

MOST_IMAGE_BYTES: Final = 12 * MEGABYTE
"""The most one image may hold, as sent."""

MOST_FILES: Final = 10
"""The most images one upload may send."""

MOST_PIXELS: Final = 30_000_000
"""The most pixels one image may declare in its header."""

LONG_EDGE: Final = 1_568
"""The longest edge a prepared image keeps."""

PROVIDER_IMAGE_LIMIT: Final = 10_000_000
"""The provider's most per image, counted as the prepared PNG's base64 length.

From the Anthropic API's vision documentation, "Image limits and costs", "Request limits",
read 2026-10-15: the most per image is 10 MB, base64-encoded, when the API is called
directly. The page doesn't say whether a megabyte is 10^6 or 2^20 bytes, so the smaller,
10,000,000 characters of base64, is kept. The same section gives 8000 x 8000 px as the most
per image, above ``LONG_EDGE``. Each image goes in a call of its own, so the request limit
isn't reached by an upload's images."""

FORMATS: Final = ("PNG", "JPEG")
"""The only plugins an image is opened with. A JPEG Pillow reads as MPO counts as a JPEG."""

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE: Final = b"\xff\xd8\xff"
HEIF_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif"}
)
"""The brands of a HEIF file, the form an iPhone keeps a photo in."""

UPLOAD_REFUSALS: Final = (
    "not_multipart",
    "body_too_large",
    "too_many_parts",
    "too_many_files",
    "file_too_large",
    "unknown_field",
    "repeated_field",
    "field_too_large",
    "unreadable",
)
"""The closed set of reasons an upload is refused whole."""

IMAGE_REFUSALS: Final = (
    "not_png_or_jpeg",
    "heic",
    "too_many_pixels",
    "several_frames",
    "undecodable",
)
"""The closed set of reasons one image of an upload is refused."""

IMAGE_LOGGER: Final = "PIL"
"""The logger Pillow writes through, with each of its modules' loggers beneath it."""

SIZE_MARKERS: Final = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
"""The JPEG markers whose segment gives the frame's height and width."""

Image.MAX_IMAGE_PIXELS = MOST_PIXELS
_decoding = threading.Lock()


class UploadRefused(Exception):
    """An upload refused whole, named by its kind alone. Nothing of it is kept."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"an upload was refused: {kind}")
        self.kind = kind


class ImageRefused(Exception):
    """One image refused, named by its kind alone."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"an image was refused: {kind}")
        self.kind = kind


class Upload(NamedTuple):
    """What an upload's body held: each named text field once, and the files in order."""

    fields: dict[str, str]
    files: tuple[bytes, ...]


class Prepared(NamedTuple):
    """A prepared PNG, its dimensions, and every size tried with its base64 length."""

    png: bytes
    width: int
    height: int
    steps: tuple[tuple[int, int, int], ...]


class PreparedUpload(NamedTuple):
    """An upload's images ready to store, and those refused, each by its place in the upload."""

    images: tuple[tuple[int, NewImage], ...]
    refused: tuple[tuple[int, str], ...]


class _Stop(Exception):
    """Raised by a parser callback once the upload is refused, to end the write."""


def quiet_the_image_library() -> None:
    """Hold Pillow's loggers above DEBUG, where its lines describe the chunks it reads."""
    logging.getLogger(IMAGE_LOGGER).setLevel(logging.INFO)


async def read_upload(
    content_type: str | None,
    content_length: str | None,
    body: AsyncIterator[bytes],
    *,
    fields: Mapping[str, int],
    file_field: str,
) -> Upload:
    """Read a multipart body from its chunks, within every limit, into memory.

    ``fields`` names each text field the form may send with its most bytes; each may come
    once. Files come only under ``file_field``. A file part with an empty name is no file.
    A declared length past the limit is refused before any byte is read; otherwise bytes are
    counted as they arrive, and the body is refused at the first chunk past the limit.
    """
    kind: bytes = b""
    options: dict[bytes, bytes] = {}
    try:
        kind, options = parse_options_header(content_type)
    except (UnicodeError, ValueError):
        kind = b""
    boundary = options.get(b"boundary", b"")
    if kind != b"multipart/form-data" or not boundary:
        refusal = "not_multipart"
        raise UploadRefused(refusal)
    if content_length is not None:
        if not (content_length.isascii() and content_length.isdigit()):
            refusal = "unreadable"
            raise UploadRefused(refusal)
        if int(content_length) > MOST_UPLOAD_BYTES:
            refusal = "body_too_large"
            raise UploadRefused(refusal)
    reader = _Reader(fields, file_field)
    parser = reader.parser(boundary)
    if parser is None:
        refusal = "unreadable"
        raise UploadRefused(refusal)
    received = 0
    async for piece in body:
        received += len(piece)
        if received > MOST_UPLOAD_BYTES:
            refusal = "body_too_large"
            raise UploadRefused(refusal)
        failed = False
        try:
            parser.write(piece)
        except _Stop:
            pass
        except FormParserError:
            failed = True
        if failed:
            refusal = "unreadable"
            raise UploadRefused(refusal)
        if reader.refusal is not None:
            raise UploadRefused(reader.refusal)
    if not reader.ended:
        refusal = "unreadable"
        raise UploadRefused(refusal)
    return Upload(dict(reader.fields), tuple(reader.files))


class _Reader:
    """The parser's callbacks: one part at a time, each checked as it arrives."""

    def __init__(self, fields: Mapping[str, int], file_field: str) -> None:
        self.limits = dict(fields)
        self.file_field = file_field
        self.most_parts = MOST_FILES + len(self.limits)
        self.fields: dict[str, str] = {}
        self.files: list[bytes] = []
        self.refusal: str | None = None
        self.ended = False
        self.parts = 0
        self.file_parts = 0
        self.headers: dict[bytes, bytes] = {}
        self.header_field = bytearray()
        self.header_value = bytearray()
        self.name: str | None = None
        self.is_file = False
        self.skipped = False
        self.data = bytearray()

    def parser(self, boundary: bytes) -> MultipartParser | None:
        failed = False
        made = None
        try:
            made = MultipartParser(
                boundary,
                {
                    "on_part_begin": self.part_begin,
                    "on_header_field": self.header_field_data,
                    "on_header_value": self.header_value_data,
                    "on_header_end": self.header_end,
                    "on_headers_finished": self.headers_finished,
                    "on_part_data": self.part_data,
                    "on_part_end": self.part_end,
                    "on_end": self.end,
                },
            )
        except FormParserError:
            failed = True
        return None if failed else made

    def refuse(self, kind: str) -> NoReturn:
        self.refusal = kind
        raise _Stop

    def part_begin(self) -> None:
        self.parts += 1
        if self.parts > self.most_parts:
            self.refuse("too_many_parts")
        self.headers = {}
        self.name = None
        self.is_file = self.skipped = False
        self.data = bytearray()

    def header_field_data(self, data: bytes, start: int, end: int) -> None:
        self.header_field += data[start:end]

    def header_value_data(self, data: bytes, start: int, end: int) -> None:
        self.header_value += data[start:end]

    def header_end(self) -> None:
        self.headers[bytes(self.header_field).lower()] = bytes(self.header_value)
        self.header_field = bytearray()
        self.header_value = bytearray()

    def headers_finished(self) -> None:
        disposition, options = parse_options_header(self.headers.get(b"content-disposition"))
        name = options.get(b"name")
        if disposition != b"form-data" or not name:
            self.refuse("unreadable")
        decoded = text_of(name)
        if decoded is None:
            self.refuse("unreadable")
        if b"filename" in options:
            if decoded != self.file_field:
                self.refuse("unknown_field")
            if options[b"filename"] == b"":
                self.skipped = True
                return
            self.file_parts += 1
            if self.file_parts > MOST_FILES:
                self.refuse("too_many_files")
            self.is_file = True
        elif decoded not in self.limits:
            self.refuse("unknown_field")
        elif decoded in self.fields:
            self.refuse("repeated_field")
        self.name = decoded

    def part_data(self, data: bytes, start: int, end: int) -> None:
        if self.skipped:
            return
        self.data += data[start:end]
        if self.is_file:
            if len(self.data) > MOST_IMAGE_BYTES:
                self.refuse("file_too_large")
        elif self.name is not None and len(self.data) > self.limits[self.name]:
            self.refuse("field_too_large")

    def part_end(self) -> None:
        if self.skipped or self.name is None:
            return
        if self.is_file:
            self.files.append(bytes(self.data))
            return
        text = text_of(bytes(self.data))
        if text is None:
            self.refuse("unreadable")
        self.fields[self.name] = text

    def end(self) -> None:
        self.ended = True


def text_of(raw: bytes) -> str | None:
    """``raw`` as UTF-8, or None when it isn't; the decode error is never kept."""
    failed = False
    text = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        failed = True
    return None if failed else text


# ------------------------------------------------------------------ preparing images


def base64_length(data: bytes) -> int:
    """How long ``data`` is once base64-encoded, padding included."""
    return 4 * ((len(data) + 2) // 3)


def prepare_upload(files: Sequence[bytes]) -> PreparedUpload:
    """Prepare each file of an upload in turn; a refused one is named by its place, and the
    others are kept."""
    images: list[tuple[int, NewImage]] = []
    refused: list[tuple[int, str]] = []
    for place, data in enumerate(files):
        kind = None
        try:
            prepared = prepare_image(data)
        except ImageRefused as refusal:
            kind = refusal.kind
        if kind is not None:
            refused.append((place, kind))
        else:
            images.append((place, as_new_image(prepared)))
    return PreparedUpload(tuple(images), tuple(refused))


def as_new_image(prepared: Prepared) -> NewImage:
    """A prepared image as the draft store takes it."""
    return NewImage(png=prepared.png, width=prepared.width, height=prepared.height)


def prepare_image(data: bytes, *, limit: int = PROVIDER_IMAGE_LIMIT) -> Prepared:
    """One image prepared to store, send and show, one decode at a time in this process."""
    kind = sniffed(data)
    if kind is not None:
        raise ImageRefused(kind)
    with _decoding:
        return _prepared(data, limit)


def sniffed(data: bytes) -> str | None:
    """Why ``data`` is no PNG or JPEG by its first bytes, or None when it may be one."""
    if data.startswith((PNG_SIGNATURE, JPEG_SIGNATURE)):
        return None
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "heic"
    return "not_png_or_jpeg"


def _prepared(data: bytes, limit: int) -> Prepared:
    """Under the decode lock: open, check, turn upright, and encode, stepping down to fit."""
    kind = None
    prepared = None
    try:
        prepared = _encoded(data, limit)
    except ImageRefused as refusal:
        kind = refusal.kind
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        kind = "too_many_pixels"
    except Exception:
        kind = "undecodable"
    if kind is not None or prepared is None:
        raise ImageRefused(kind or "undecodable")
    return prepared


def declared_size(data: bytes) -> tuple[int, int] | None:
    """The width and height a PNG's header or a JPEG's frame segment declares, read before
    Pillow opens it, or None when they can't be found; Pillow then reads the header itself."""
    if data.startswith(PNG_SIGNATURE):
        if data[12:16] != b"IHDR" or len(data) < 24:
            return None
        return int.from_bytes(data[16:20]), int.from_bytes(data[20:24])
    at = 2
    while at + 4 <= len(data) and data[at] == 0xFF:
        marker = data[at + 1]
        if marker == 0xFF:
            at += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            at += 2
            continue
        if marker in SIZE_MARKERS:
            if at + 9 > len(data):
                return None
            return int.from_bytes(data[at + 7 : at + 9]), int.from_bytes(data[at + 5 : at + 7])
        length = int.from_bytes(data[at + 2 : at + 4])
        if marker in {0xD9, 0xDA} or length < 2:
            return None
        at += 2 + length
    return None


def _encoded(data: bytes, limit: int) -> Prepared:
    # The declared size is checked before Pillow opens the image, since Pillow warns on its
    # own past the same limit, on stderr, before a check after the open could refuse it.
    declared = declared_size(data)
    if declared is not None and declared[0] * declared[1] > MOST_PIXELS:
        refusal = "too_many_pixels"
        raise ImageRefused(refusal)
    with Image.open(io.BytesIO(data), formats=FORMATS) as opened:
        width, height = opened.size
        if width * height > MOST_PIXELS:
            refusal = "too_many_pixels"
            raise ImageRefused(refusal)
        jpeg = isinstance(opened, JpegImagePlugin.JpegImageFile)
        if isinstance(opened, PngImagePlugin.PngImageFile) and opened.n_frames > 1:
            refusal = "several_frames"
            raise ImageRefused(refusal)
        opened.seek(0)
        opened.load()
        if jpeg:
            ImageOps.exif_transpose(opened, in_place=True)
        upright: Image.Image = opened
        if upright.mode.startswith("I"):
            # Wide gray, 16 bits a pixel: scaled to 8 bits rather than clipped to white.
            upright = upright.convert("I").point(lambda value: value * (1 / 257), "L")
        alpha = upright.mode in {"RGBA", "LA", "PA"} or "transparency" in upright.info
        pixels = upright.convert("RGBA" if alpha else "RGB")
    return stepped(pixels, limit)


def stepped(pixels: Image.Image, limit: int) -> Prepared:
    """Encode ``pixels`` at a long edge of at most ``LONG_EDGE``, then at strictly smaller
    dimensions until the PNG's base64 length is within ``limit``; the first that fits."""
    width, height = pixels.size
    longest = max(width, height)
    edge = min(longest, LONG_EDGE)
    steps: list[tuple[int, int, int]] = []
    while True:
        size = scaled(width, height, edge)
        frame = pixels if size == pixels.size else pixels.resize(size, Image.Resampling.LANCZOS)
        frame.info = {}
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        png = buffer.getvalue()
        length = base64_length(png)
        steps.append((size[0], size[1], length))
        if length <= limit:
            return Prepared(png, size[0], size[1], tuple(steps))
        if edge == 1:
            msg = "the size limit is below a one-pixel PNG"
            raise ValueError(msg)
        edge = max(1, min(edge * 85 // 100, edge - 1))


def scaled(width: int, height: int, edge: int) -> tuple[int, int]:
    """``width`` x ``height`` with its long edge ``edge``, the short edge at least one pixel."""
    if width >= height:
        return edge, max(1, round(height * edge / width))
    return max(1, round(width * edge / height)), edge
