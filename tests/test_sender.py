"""Lossless transport checks; no third-party libraries or camera simulation."""

import hashlib
import os
import select
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "send.sh"
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"
# Derive sextant masks independently from Unicode names, not the sender table.
SEXTANTS = {" ": 0, "▌": 21, "▐": 42, "█": 63}
for point in range(0x1FB00, 0x1FB3C):
    char = chr(point)
    SEXTANTS[char] = sum(
        1 << (int(p) - 1) for p in unicodedata.name(char).split("-")[1]
    )


def decode_transcript(transcript, mode):
    screens = transcript.split("\f")
    assert screens.pop() == ""
    result = bytearray()
    pages = 0
    kinds = []
    bits = 6 if mode == "sextant" else 4
    masks = SEXTANTS if bits == 6 else {c: i for i, c in enumerate(QUADRANTS)}
    for screen in screens:
        rows = screen.splitlines()
        assert len(rows) == 24 and all(len(r) == 80 for r in rows)
        assert rows[0] == rows[23] == "█" * 80
        assert rows[1] == rows[2] == rows[22]
        header_hex = "".join(format(QUADRANTS.index(c), "x") for c in rows[1][2:66])
        header = bytes.fromhex(header_hex)
        assert header[:4] == b"CB01" and header[5] == bits
        kind = chr(header[4])
        kinds.append(kind)
        index = int.from_bytes(header[6:14], "big")
        length = int.from_bytes(header[14:16], "big")
        body = "".join(r[2:78] for r in rows[4:22])
        payload = b""
        if kind == "C":
            assert [masks[c] for c in body] == [i % (1 << bits) for i in range(1368)]
        elif kind == "D":
            assert index == pages
            stream = "".join(format(masks[c], f"0{bits}b") for c in body)
            payload = bytes(int(stream[i : i + 8], 2) for i in range(0, length * 8, 8))
            assert set(stream[length * 8 :]) <= {"0"}
            result.extend(payload)
            pages += 1
        else:
            assert kind == "E" and index == pages and length == 0
        expected = hashlib.sha256(header_hex[:32].encode() + payload).digest()[:16]
        assert expected == header[16:]
    assert kinds == ["C"] * 4 + ["D"] * pages + ["E"] * 3
    return bytes(result)


class SenderTests(unittest.TestCase):
    def test_binary_roundtrip(self):
        for mode, capacity in [("sextant", 1026), ("quadrant", 684)]:
            for size in [
                0,
                1,
                2,
                3,
                capacity - 1,
                capacity,
                capacity + 1,
                capacity * 2 + 7,
            ]:
                with self.subTest(mode=mode, size=size):
                    data = (bytes(range(256)) * 20)[:size]
                    run = subprocess.run(
                        ["bash", str(SCRIPT), "--mode", mode, "--dump", "-"],
                        input=data,
                        capture_output=True,
                        check=True,
                    )
                    self.assertEqual(decode_transcript(run.stdout.decode(), mode), data)

    def test_input_error_does_not_emit_eof(self):
        run = subprocess.run(
            ["bash", str(SCRIPT), "--dump", "/tmp"], capture_output=True
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.stdout, b"")

    def test_read_failure_after_partial_input_does_not_emit_end(self):
        with tempfile.TemporaryDirectory() as directory:
            encoder = Path(directory) / "base64"
            real_encoder = shlex.quote(shutil.which("base64"))
            encoder.write_text(
                "#!/bin/bash\nif [[ $1 == -w0 ]]; then printf QUJD; exit 9; fi\n"
                f'exec {real_encoder} "$@"\n'
            )
            encoder.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dump", "-"],
                input=b"",
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"},
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout.decode().count("\f"), 5)
            self.assertIn(b"Input encoding failed", result.stderr)

    def test_streams_before_eof_and_stops_on_interrupt(self):
        process = subprocess.Popen(
            ["bash", str(SCRIPT), "--dump", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def feed():
            try:
                process.stdin.write(b"x" * 65536)
                process.stdin.flush()
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=feed)
        writer.start()
        try:
            transcript = b""
            deadline = time.monotonic() + 5
            while transcript.count(b"\f") < 5 and time.monotonic() < deadline:
                if select.select([process.stdout], [], [], 0.1)[0]:
                    transcript += os.read(process.stdout.fileno(), 65536)
            self.assertGreaterEqual(transcript.count(b"\f"), 5)
            self.assertIsNone(process.poll(), "Sender must still be waiting for input")
        finally:
            process.terminate()
            process.wait(timeout=5)
            writer.join(timeout=5)
            process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143)


if __name__ == "__main__":
    unittest.main(verbosity=2)
