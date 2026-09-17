import base64
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu.media import (  # noqa: E402
    DEFAULT_DISPLAY_WIDTH,
    MAX_IMAGE_BYTES,
    MediaError,
    encode_image_base64,
    image_upload_payload,
    read_image_dimensions,
    validate_display_width,
)


def png_header(width=320, height=240):
    signature = b"\x89PNG\r\n\x1a\n"
    data = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + data
    return signature + chunk + struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)


def jpeg_header(width=640, height=480):
    # SOI, then a baseline SOF0 segment. No entropy-coded image is needed
    # for the dimension parser, but the segment itself is complete.
    sof = bytes((8,)) + struct.pack(">HH", height, width) + bytes((3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1))
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof


class TestImageDimensions(unittest.TestCase):
    def test_reads_png_dimensions_from_bytes(self):
        self.assertEqual(read_image_dimensions(png_header()), (320, 240))

    def test_reads_jpeg_dimensions_from_bytes(self):
        self.assertEqual(read_image_dimensions(jpeg_header()), (640, 480))

    def test_reads_dimensions_from_a_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.png"
            path.write_bytes(png_header(17, 23))
            self.assertEqual(read_image_dimensions(path), (17, 23))

    def test_rejects_empty_and_unsupported_bytes(self):
        for data in (b"", b"GIF89a", b"not an image"):
            with self.subTest(data=data), self.assertRaises(MediaError):
                read_image_dimensions(data)

    def test_rejects_truncated_png(self):
        data = png_header()[:-1]
        with self.assertRaisesRegex(MediaError, "PNG"):
            read_image_dimensions(data)

    def test_rejects_bad_png_signature_and_crc(self):
        bad_signature = b"x" + png_header()[1:]
        bad_crc = png_header()[:-1] + bytes((png_header()[-1] ^ 1,))
        for data in (bad_signature, bad_crc):
            with self.subTest(data=data), self.assertRaises(MediaError):
                read_image_dimensions(data)

    def test_rejects_zero_sized_png(self):
        with self.assertRaisesRegex(MediaError, "尺寸"):
            read_image_dimensions(png_header(0, 1))

    def test_rejects_truncated_jpeg_segment(self):
        data = b"\xff\xd8\xff\xc0\x00\x10\x08\x00"
        with self.assertRaisesRegex(MediaError, "JPEG"):
            read_image_dimensions(data)

    def test_rejects_jpeg_without_a_dimensions_frame(self):
        # DQT is a valid marker, but it does not contain image dimensions.
        data = b"\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xd9"
        with self.assertRaisesRegex(MediaError, "JPEG"):
            read_image_dimensions(data)

    def test_rejects_jpeg_segment_with_invalid_length(self):
        data = b"\xff\xd8\xff\xc0\x00\x01"
        with self.assertRaisesRegex(MediaError, "JPEG"):
            read_image_dimensions(data)

    def test_rejects_directory_and_missing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in (Path(directory), Path(directory) / "missing.png"):
                with self.subTest(path=path), self.assertRaises(MediaError):
                    read_image_dimensions(path)


class TestImageBase64(unittest.TestCase):
    def test_encodes_exact_local_file_bytes(self):
        data = png_header(5, 7)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.png"
            path.write_bytes(data)
            self.assertEqual(encode_image_base64(path), base64.b64encode(data).decode("ascii"))

    def test_builds_the_upload_payload_with_only_pure_base64(self):
        data = jpeg_header(5, 7)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.jpg"
            path.write_bytes(data)
            self.assertEqual(image_upload_payload(path), {"data": base64.b64encode(data).decode("ascii")})

    def test_base64_rejects_invalid_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.gif"
            path.write_bytes(b"GIF89a")
            with self.assertRaises(MediaError):
                encode_image_base64(path)

    def test_rejects_oversized_local_file_before_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.png"
            path.write_bytes(png_header() + b"x" * MAX_IMAGE_BYTES)
            with self.assertRaisesRegex(MediaError, "过大"):
                encode_image_base64(path)


class TestDisplayWidth(unittest.TestCase):
    def test_none_uses_the_protocol_default(self):
        self.assertEqual(validate_display_width(None), DEFAULT_DISPLAY_WIDTH)

    def test_accepts_positive_integer_width(self):
        self.assertEqual(validate_display_width(1), 1)
        self.assertEqual(validate_display_width(400), 400)

    def test_rejects_zero_negative_non_integer_and_bool_widths(self):
        for width in (0, -1, 1.5, "400", True, False):
            with self.subTest(width=width), self.assertRaises(ValueError):
                validate_display_width(width)


if __name__ == "__main__":
    unittest.main()
