"""Cached geometry must remain fast when stable and recover immediately on motion."""

import subprocess
import unittest
from unittest.mock import patch

import numpy as np
from test_receiver import render
from test_sender import SCRIPT

import receive as rx


class TrackingTests(unittest.TestCase):
    def test_stable_geometry_skips_search_but_motion_reacquires_same_frame(self):
        source = np.random.default_rng(123).bytes(2052)
        screens = (
            subprocess.run(
                ["bash", str(SCRIPT), "--dump", "-"],
                input=source,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .split("\f")[4:6]
        )
        first, second = [np.pad(render(screen, "sextant"), 40) for screen in screens]
        decoder = rx.Decoder()
        self.assertEqual(decoder.frame(first)[0][1], source[:1026])
        with patch.object(
            rx, "find_panels", side_effect=AssertionError("Unneeded search")
        ):
            self.assertEqual(decoder.frame(first), [])
            self.assertEqual(decoder.frame(second)[0][1], source[1026:])
        # Fresh decoder retains the first page geometry, but the next page moves.
        decoder = rx.Decoder()
        decoder.frame(first)
        moved = np.roll(second, (15, 20), axis=(0, 1))
        with patch.object(rx, "find_panels", wraps=rx.find_panels) as search:
            self.assertEqual(decoder.frame(moved)[0][1], source[1026:])
            search.assert_called_once()
