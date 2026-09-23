"""Exercise the mobile bridge with real camera data and terminal screens."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from test_receiver import FIXTURES, render
from test_recovery import packet
from test_sender import SCRIPT

from live_receive import LiveReceiver


class LiveReceiverTests(unittest.TestCase):
    def test_completion_without_hard_link_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = LiveReceiver(directory)
            try:
                packets = [
                    (packet(b"test data"), b"test data"),
                    (packet(kind="E", index=1), b""),
                ]
                with (
                    patch.object(receiver.decoder, "frame", return_value=packets),
                    patch(
                        "receive.os.link",
                        side_effect=PermissionError("Permission denied"),
                    ),
                ):
                    report = receiver.accept_frame(None)
                self.assertEqual(report["status"], "complete", report)
                self.assertEqual(
                    Path(receiver.completed_path()).read_bytes(), b"test data"
                )
                self.assertFalse(
                    list(Path(receiver.directory.name).glob(".received.bin-*"))
                )
            finally:
                receiver.close()

    def test_camera_luma_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = LiveReceiver(directory)
            self.addCleanup(receiver.close)
            capture = cv2.VideoCapture(str(FIXTURES / "small.mp4"))
            self.addCleanup(capture.release)
            while True:
                ok, image = capture.read()
                if not ok:
                    break
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                height, width = gray.shape
                report = json.loads(receiver.accept_luma(gray.tobytes(), width, height))
                if report["status"] == "complete":
                    break
            self.assertEqual(report["status"], "complete", report)
            self.assertEqual(
                Path(receiver.completed_path()).read_bytes(),
                (FIXTURES / "small.bin").read_bytes(),
            )
            temporary = Path(receiver.directory.name)
            receiver.close()
            self.assertFalse(temporary.exists())

    def test_modes_rotations_and_empty_transfer(self):
        for mode, source in [("quadrant", b""), ("sextant", bytes(range(256)) * 5)]:
            screens = (
                subprocess.run(
                    ["bash", str(SCRIPT), "--mode", mode, "--dump", "-"],
                    input=source,
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .split("\f")[:-1]
            )
            for rotation in (0, 90, 180, 270):
                with (
                    self.subTest(mode=mode, rotation=rotation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    receiver = LiveReceiver(directory)
                    try:
                        for screen in screens:
                            frame = np.pad(render(screen, mode), 30)
                            frame = np.rot90(frame, rotation // 90)
                            height, width = frame.shape
                            report = json.loads(
                                receiver.accept_luma(
                                    frame.tobytes(), width, height, rotation
                                )
                            )
                        self.assertEqual(report["status"], "complete", report)
                        self.assertEqual(
                            Path(receiver.completed_path()).read_bytes(), source
                        )
                    finally:
                        receiver.close()

    def test_incomplete_error_and_export_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = LiveReceiver(directory, max_bytes=1026)
            try:
                with patch.object(
                    receiver.decoder, "frame", return_value=[(packet(b"x"), b"x")]
                ):
                    self.assertEqual(receiver.accept_frame(None)["checked_pages"], 1)
                    self.assertEqual(receiver.accept_frame(None)["checked_pages"], 1)
                with self.assertRaisesRegex(ValueError, "not complete"):
                    receiver.completed_path()
                with (
                    patch.object(
                        receiver.decoder,
                        "frame",
                        return_value=[(packet(kind="E", index=1), b"")],
                    ),
                    patch.object(
                        receiver.recovery, "write", side_effect=OSError("disk full")
                    ),
                ):
                    self.assertEqual(receiver.accept_frame(None)["status"], "error")
                with self.assertRaisesRegex(ValueError, "not complete"):
                    receiver.completed_path()
            finally:
                receiver.close()

    def test_database_limit_stops_reception_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = LiveReceiver(directory)
            try:
                receiver.recovery.db.execute("PRAGMA max_page_count=3")
                data = b"x" * 1026
                for index in range(20):
                    with patch.object(
                        receiver.decoder,
                        "frame",
                        return_value=[(packet(data, index=index), data)],
                    ):
                        report = receiver.accept_frame(None)
                    if report["status"] == "error":
                        break
                self.assertEqual(report["status"], "error")
                self.assertIn("full", report["error"])
                self.assertFalse(receiver.output.exists())
            finally:
                receiver.close()

    def test_bad_buffers(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = LiveReceiver(directory)
            try:
                for args in [(b"", 0, 1), (b"x", 2, 2), (b"x", 1, 1, 45)]:
                    with self.assertRaises(ValueError):
                        receiver.accept_luma(*args)
            finally:
                receiver.close()
