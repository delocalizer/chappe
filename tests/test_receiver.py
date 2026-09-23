"""Exercise real sender output through compressed, perturbed synthetic video."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from test_sender import QUADRANTS, SCRIPT, SEXTANTS

import receive as rx

RECEIVER = Path(__file__).resolve().parents[1] / "receive.py"
FIXTURES = Path(__file__).with_name("fixtures")


def render(screen, mode):
    """Render ideal block geometry, independently of receiver sampling code."""
    result = np.zeros((576, 960), np.uint8)
    for row, line in enumerate(screen.splitlines()):
        for col, char in enumerate(line):
            bits = 6 if mode == "sextant" and 4 <= row < 22 and 2 <= col < 78 else 4
            mask = SEXTANTS[char] if bits == 6 else QUADRANTS.index(char)
            height = 24 // (bits // 2)
            for bit in range(bits):
                if mask & (1 << bit):
                    y = row * 24 + (bit // 2) * height
                    x = col * 12 + (bit % 2) * 6
                    result[y : y + height, x : x + 6] = 235
    return result


def film(screens, mode, output, rotate=False, clip_corner=False, decoration=False):
    rng = np.random.default_rng(42)
    size = (720, 1280) if rotate else (1280, 720)
    video = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), 30, size)
    assert video.isOpened()
    previous = None
    for screen in screens:
        panel = render(screen, mode)
        for index in range(4):
            displayed = panel.copy()
            if previous is not None and index == 0:
                displayed[288:] = previous[288:]
            quad = np.float32([[140, 65], [1100, 85], [1080, 660], [130, 630]])
            if clip_corner:
                # The lower-left border corner is outside the frame, but all
                # payload cells and enough of each border edge remain visible.
                quad[2:] = [[1080, 690], [130, 735]]
            quad += rng.normal(0, 0.5, quad.shape).astype(np.float32)
            transform = cv2.getPerspectiveTransform(
                np.float32([[0, 0], [959, 0], [959, 575], [0, 575]]), quad
            )
            if decoration:
                # A dimmer scrollbar joins the white border through a narrow
                # bridge. At Otsu's cutoff the outline is not a quadrilateral;
                # brighter thresholds must separate it from the actual panel.
                canvas = np.full((576, 980), 25, np.uint8)
                canvas[:, :960] = displayed
                canvas[20:555, 966:974] = 150
                canvas[280:286, 958:974] = 150
                displayed = canvas
            image = cv2.warpPerspective(
                displayed, transform, (1280, 720), borderValue=25
            )
            image = cv2.GaussianBlur(image, (5, 5), 0.65)
            image = np.clip(
                image.astype(float) + rng.normal(0, 2, image.shape), 0, 255
            ).astype(np.uint8)
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            if rotate:
                image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
            video.write(image)
        previous = panel
    video.release()


class ReceiverTests(unittest.TestCase):
    def test_camera_roundtrips(self):
        for mode, rotate, clip_corner, source in [
            ("sextant", False, False, bytes(range(256)) * 5 + b"\0tail"),
            ("quadrant", True, False, bytes(range(256)) * 3 + b"\xff"),
            ("sextant", False, True, bytes(range(256)) * 5 + b"\0tail"),
            ("quadrant", True, True, bytes(range(256)) * 3 + b"\xff"),
        ]:
            with (
                self.subTest(mode=mode, clip_corner=clip_corner),
                tempfile.TemporaryDirectory() as tmp,
            ):
                tmp = Path(tmp)
                run = subprocess.run(
                    ["bash", str(SCRIPT), "--mode", mode, "--dump", "-"],
                    input=source,
                    capture_output=True,
                    check=True,
                )
                # Calibration is helpful for human inspection but decoding is geometric.
                screens = run.stdout.decode().split("\f")[4:-1]
                video = tmp / "camera.mp4"
                film(screens, mode, video, rotate, clip_corner)
                output = tmp / "output"
                result = subprocess.run(
                    [sys.executable, str(RECEIVER), str(video), "-o", str(output)],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes(), source)

    def test_empty_video_transfer_and_missing_page(self):
        for source, remove_page in [(b"", False), (b"x" * 2500, True)]:
            with (
                self.subTest(remove_page=remove_page),
                tempfile.TemporaryDirectory() as tmp,
            ):
                tmp = Path(tmp)
                run = subprocess.run(
                    ["bash", str(SCRIPT), "--dump", "-"],
                    input=source,
                    capture_output=True,
                    check=True,
                )
                screens = run.stdout.decode().split("\f")[4:-1]
                if remove_page:
                    del screens[1]
                video = tmp / "camera.mp4"
                film(screens, "sextant", video)
                output = tmp / "output"
                output.write_bytes(b"keep me")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(RECEIVER),
                        str(video),
                        "-o",
                        str(output),
                        "--force",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(
                    result.returncode, 2 if remove_page else 0, result.stderr
                )
                self.assertEqual(
                    output.read_bytes(), b"keep me" if remove_page else b""
                )

    def test_corruption_and_conflicting_pages(self):
        def header(data, index=0, kind="D"):
            prefix = (
                b"CB01"
                + kind.encode()
                + b"\x06"
                + index.to_bytes(8, "big")
                + len(data).to_bytes(2, "big")
            )
            import hashlib

            raw = prefix + hashlib.sha256(prefix.hex().encode() + data).digest()[:16]
            return rx.parse_header(raw)

        with tempfile.TemporaryDirectory() as tmp:
            recovery = rx.Recovery(str(Path(tmp) / "pages"), 10000)
            try:
                with self.assertRaisesRegex(ValueError, "Unchecked"):
                    recovery.accept(header(b"a"), b"b")
                recovery.accept(header(b"a"), b"a")
                with self.assertRaisesRegex(ValueError, "Conflicting checked"):
                    recovery.accept(header(b"b"), b"b")
                self.assertFalse(recovery.report()["end_seen"])
                self.assertEqual(recovery.report()["status"], "incomplete")
            finally:
                recovery.db.close()

    def test_temporal_average_and_redundant_headers(self):
        source = bytes(range(256))
        run = subprocess.run(
            ["bash", str(SCRIPT), "--dump", "-"],
            input=source,
            capture_output=True,
            check=True,
        )
        panel = render(run.stdout.decode().split("\f")[4], "sextant")
        decoder = rx.Decoder()
        found = []
        for column in (3, 5, 7):
            frame = panel.copy()
            # Destroy one header copy and a different full body cell each time.
            frame[24:48, 24:792] = 0
            frame[96:120, column * 12 : (column + 1) * 12] = (
                235 - frame[96:120, column * 12 : (column + 1) * 12]
            )
            frame = cv2.copyMakeBorder(
                frame, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=25
            )
            found.extend(decoder.frame(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)))
        self.assertTrue(
            found,
            "Temporal averaging failed to repair independently damaged observations",
        )
        self.assertEqual(found[-1][1], source)

    def test_brightness_spill_requires_checksum_verified_threshold_retry(self):
        source = bytes(range(256)) * 4
        run = subprocess.run(
            ["bash", str(SCRIPT), "--dump", "-"],
            input=source,
            capture_output=True,
            check=True,
        )
        panel = render(run.stdout.decode().split("\f")[4], "sextant")
        # Mimic camera/glyph spill: black subregions on the right become brighter
        # than the fixed midpoint, while the reference row remains unaffected.
        region = panel[96:528, 720:936]
        region[region == 0] = 155
        frame = cv2.copyMakeBorder(panel, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=25)
        found = rx.Decoder().frame(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
        self.assertTrue(found)
        self.assertEqual(found[0][1], source)
        header = found[0][0]
        light = np.full((18, 76, 6), 0.5, np.float32)
        self.assertIsNone(
            rx.checked_payload(header, light),
            "Never accept an unchecked threshold guess",
        )

    @unittest.skipUnless(
        (FIXTURES / "small.mp4").exists() and (FIXTURES / "small.bin").exists(),
        "Local camera fixtures unavailable",
    )
    def test_actual_camera_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "recovered"
            result = subprocess.run(
                [
                    sys.executable,
                    str(RECEIVER),
                    str((FIXTURES / "small.mp4")),
                    "-o",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), (FIXTURES / "small.bin").read_bytes())

    def test_synthetic_video_with_adjacent_window_decoration(self):
        source = np.random.default_rng(20260923).bytes(25100)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = subprocess.run(
                ["bash", str(SCRIPT), "--dump", "-"],
                input=source,
                capture_output=True,
                check=True,
            )
            screens = run.stdout.decode().split("\f")[4:-1]
            video = tmp / "decoration.mp4"
            film(screens, "sextant", video, decoration=True)
            output = tmp / "recovered"
            result = subprocess.run(
                [
                    sys.executable,
                    str(RECEIVER),
                    str(video),
                    "-o",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
