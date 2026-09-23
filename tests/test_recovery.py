"""Integrity, resource limits, and filesystem protections."""

import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import receive as rx


def packet(data=b"", kind="D", index=0, length=None):
    if length is None:
        length = len(data)
    prefix = b"CB01" + kind.encode() + b"\x06"
    prefix += index.to_bytes(8, "big") + length.to_bytes(2, "big")
    digest = hashlib.sha256(prefix.hex().encode() + data).digest()[:16]
    return rx.parse_header(prefix + digest)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.recovery = rx.Recovery(str(self.root / "pages.sqlite"), 1026)
        self.addCleanup(self.recovery.db.close)

    def test_checksum_does_not_override_declared_length(self):
        header = packet(b"x", length=2)
        self.assertFalse(header.checks(b"x"))
        with self.assertRaisesRegex(ValueError, "Unchecked"):
            self.recovery.accept(header, b"x")

    def test_output_requires_complete_transfer_and_respects_existing_file(self):
        output = self.root / "output"
        output.write_bytes(b"original")
        self.recovery.accept(packet(b"new"), b"new")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.recovery.write(output, force=True)
        self.assertEqual(output.read_bytes(), b"original")
        self.recovery.accept(packet(kind="E", index=1), b"")
        with self.assertRaises(FileExistsError):
            self.recovery.write(output, force=False)
        self.assertEqual(output.read_bytes(), b"original")
        self.recovery.write(output, force=True)
        self.assertEqual(output.read_bytes(), b"new")

    def test_end_count_and_sqlite_index_limits(self):
        with self.assertRaisesRegex(ValueError, "max-bytes"):
            self.recovery.accept(packet(kind="E", index=2), b"")
        self.recovery.max_bytes = 2**90
        with self.assertRaisesRegex(ValueError, "storage limits"):
            self.recovery.accept(packet(b"x", index=2**63), b"x")

    def test_hard_link_paths_are_rejected(self):
        video = self.root / "video"
        video.write_bytes(b"original")
        report = self.root / "report"
        os.link(video, report)
        with self.assertRaisesRegex(ValueError, "different files"):
            rx.validate_paths((video, self.root / "output", report))
        self.assertEqual(video.read_bytes(), b"original")

    def test_repeated_geometry_candidates_do_not_duplicate_frame_weight(self):
        header = packet(b"data")
        nibbles = np.unpackbits(np.frombuffer(header.raw, np.uint8)).reshape(64, 4)
        header_light = np.repeat(nibbles[None, :, ::-1], 3, axis=0)
        body = np.full((18, 76, 6), 0.5, np.float32)

        def sample(panel, rows, columns, bits):
            return header_light if len(rows) == 3 else body

        decoder = rx.Decoder()
        quad = np.float32([[0, 0], [100, 0], [100, 100], [0, 100]])
        with (
            patch.object(rx, "find_panels", return_value=[quad, quad]),
            patch.object(rx, "rectify", return_value=np.zeros((576, 960))),
            patch.object(rx, "sample_cells", side_effect=sample),
        ):
            self.assertEqual(decoder.frame(np.zeros((100, 100, 3), np.uint8)), [])
        self.assertEqual(decoder.pending[header.raw][1], 1)
        self.assertEqual(decoder.panels, 1)

    def test_missing_end_message_is_only_shown_when_end_is_missing(self):
        self.recovery.accept(packet(kind="E", index=1), b"")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rx.print_result(self.recovery.report(), self.root / "output")
        self.assertIn("Missing pages: 0", stderr.getvalue())
        self.assertNotIn("unknown", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
