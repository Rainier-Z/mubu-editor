"""Pure local helpers for validating and preparing PNG/JPEG image uploads.

The Mubu upload endpoint accepts a JSON object containing a plain Base64
string.  This module deliberately has no network or Mubu-client dependency:
callers must provide a local path for upload data, and dimensions are parsed
from the image header without Pillow or another third-party decoder.
"""

from __future__ import annotations

import base64
import os
import struct
import zlib
from pathlib import Path
from typing import Union

DEFAULT_DISPLAY_WIDTH = 400
# Keep local uploads bounded before they are copied into memory and sent to
# the remote object store.  The limit is deliberately explicit and shared by
# all file based image helpers.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = frozenset(
    (*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0))
)
_JPEG_STANDALONE_MARKERS = frozenset(
    (0x01, 0xD8, 0xD9, *range(0xD0, 0xD8))
)
_ImageSource = Union[bytes, bytearray, memoryview, str, os.PathLike[str]]


class MediaError(ValueError):
    """Raised when local image data or an image option is invalid."""


def _read_local_file(path: Union[str, os.PathLike[str]]) -> bytes:
    try:
        local_path = Path(path)
    except (TypeError, ValueError) as exc:
        raise MediaError("图片路径必须是本地文件路径") from exc

    try:
        if not local_path.is_file():
            raise MediaError(f"图片路径不是文件：{local_path}")
        with local_path.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise MediaError(
                f"图片文件过大（上限 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB）：{local_path}"
            )
        return data
    except OSError as exc:
        raise MediaError(f"无法读取本地图片：{local_path}") from exc


def _source_bytes(source: _ImageSource) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        if len(data) > MAX_IMAGE_BYTES:
            raise MediaError(
                f"图片数据过大（上限 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB）"
            )
        return data
    return _read_local_file(source)


def _valid_dimensions(width: int, height: int, format_name: str) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise MediaError(f"{format_name} 图片尺寸必须为正数")
    return width, height


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < len(_PNG_SIGNATURE):
        raise MediaError("PNG 数据不完整")
    if data[:8] != _PNG_SIGNATURE:
        raise MediaError("无效的 PNG 文件头")
    if len(data) < 16:
        raise MediaError("PNG IHDR 数据不完整")

    chunk_length = struct.unpack(">I", data[8:12])[0]
    if data[12:16] != b"IHDR":
        raise MediaError("PNG 首个数据块必须是 IHDR")
    if chunk_length != 13:
        raise MediaError("PNG IHDR 长度无效")

    chunk_end = 16 + chunk_length
    if len(data) < chunk_end + 4:
        raise MediaError("PNG IHDR 数据截断")
    chunk_data = data[16:chunk_end]
    expected_crc = struct.unpack(">I", data[chunk_end:chunk_end + 4])[0]
    actual_crc = zlib.crc32(data[12:16] + chunk_data) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise MediaError("PNG IHDR 校验失败")

    width, height = struct.unpack(">II", chunk_data[:8])
    return _valid_dimensions(width, height, "PNG")


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 2 or data[:2] != b"\xff\xd8":
        raise MediaError("无效的 JPEG 文件头")

    position = 2
    while position < len(data):
        if data[position] != 0xFF:
            raise MediaError("JPEG 标记数据截断或格式无效")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            raise MediaError("JPEG 标记数据截断")

        marker = data[position]
        position += 1
        if marker == 0x00:
            raise MediaError("JPEG 标记格式无效")
        if marker in _JPEG_SOF_MARKERS:
            if position + 2 > len(data):
                raise MediaError("JPEG SOF 数据截断")
            segment_length = struct.unpack(">H", data[position:position + 2])[0]
            if segment_length < 8:
                raise MediaError("JPEG SOF 长度无效")
            segment_end = position + segment_length
            if segment_end > len(data):
                raise MediaError("JPEG SOF 数据截断")
            payload = data[position + 2:segment_end]
            if len(payload) < 6:
                raise MediaError("JPEG SOF 数据不完整")
            height, width = struct.unpack(">HH", payload[1:5])
            return _valid_dimensions(width, height, "JPEG")

        if marker in _JPEG_STANDALONE_MARKERS:
            if marker == 0xD9:
                break
            continue

        if position + 2 > len(data):
            raise MediaError("JPEG 段长度数据截断")
        segment_length = struct.unpack(">H", data[position:position + 2])[0]
        if segment_length < 2:
            raise MediaError("JPEG 段长度无效")
        segment_end = position + segment_length
        if segment_end > len(data):
            raise MediaError("JPEG 段数据截断")
        if marker == 0xDA:
            break
        position = segment_end

    raise MediaError("JPEG 中未找到有效的图像尺寸帧")


def read_image_dimensions(source: _ImageSource) -> tuple[int, int]:
    """Read ``(width, height)`` from local PNG/JPEG bytes or a local path.

    Only the PNG signature/IHDR or JPEG SOF frame is inspected.  Corrupt,
    truncated, unsupported, and zero-sized images raise :class:`MediaError`.
    """
    data = _source_bytes(source)
    if data.startswith(_PNG_SIGNATURE):
        return _png_dimensions(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(data)
    raise MediaError("只支持 PNG 或 JPEG 图片")


def encode_image_base64(path: Union[str, os.PathLike[str]]) -> str:
    """Validate and Base64-encode bytes read from one local image file."""
    data = _read_local_file(path)
    read_image_dimensions(data)
    return base64.b64encode(data).decode("ascii")


def image_upload_payload(path: Union[str, os.PathLike[str]]) -> dict[str, str]:
    """Return the upload JSON payload for one validated local image file."""
    return {"data": encode_image_base64(path)}


def validate_display_width(width: object = None) -> int:
    """Validate a positive integer display width, defaulting to 400 pixels."""
    if width is None:
        return DEFAULT_DISPLAY_WIDTH
    if type(width) is not int or width <= 0:
        raise ValueError("图片显示宽度必须是正整数")
    return width


__all__ = [
    "DEFAULT_DISPLAY_WIDTH",
    "MAX_IMAGE_BYTES",
    "MediaError",
    "encode_image_base64",
    "image_upload_payload",
    "read_image_dimensions",
    "validate_display_width",
]


def read_image_details(source: _ImageSource) -> tuple:
    """一次读取图片：返回 ``(原始字节, 宽, 高, 扩展名, Content-Type)``。

    仅支持 PNG / JPEG；用于 TOS 直传（需要原始字节与正确的 Content-Type）。
    """
    data = _source_bytes(source)
    if bytes(data[:8]) == _PNG_SIGNATURE:
        ext, content_type = "png", "image/png"
        width, height = _png_dimensions(data)
    elif bytes(data[:2]) == b"\xff\xd8":
        ext, content_type = "jpg", "image/jpeg"
        width, height = _jpeg_dimensions(data)
    else:
        raise MediaError("仅支持 PNG / JPEG 图片")
    width, height = _valid_dimensions(width, height, ext)
    return bytes(data), width, height, ext, content_type
