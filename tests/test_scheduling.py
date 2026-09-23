"""Verify terminal pacing with a deterministic clock and construction delays."""

import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "send.sh"

# Only test executables use Python. The sender still runs Bash/coreutils.
CLOCK_TOOL = """#!{python}
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

path = Path(os.environ["CHAPPE_TEST_CLOCK"])
state = json.loads(path.read_text())
tool = Path(sys.argv[0]).name
if tool == "date":
    state["dates"] += 1
    print("CLOCK:" + str(state["now"]), file=sys.stderr, flush=True)
    print(state["now"])
elif tool == "sleep":
    duration = int(Decimal(sys.argv[1]) * 1_000_000_000)
    state["sleeps"].append(duration)
    state["now"] += duration
else:
    # Calibration, first data page, delayed second data page, then normal pages.
    state["now"] += 250_000_000 if state["hashes"] == 2 else 30_000_000
    state["hashes"] += 1
path.write_text(json.dumps(state))
if tool == "sha256sum":
    os.execv({sha!r}, [{sha!r}, *sys.argv[1:]])
"""


class SchedulingTests(unittest.TestCase):
    def test_preparation_overlaps_hold_without_catch_up(self):
        for interval in (".1", "0"):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                source = directory / "source"
                source.write_bytes(bytes(range(256)) * 9)
                clock = directory / "clock.json"
                clock.write_text(
                    json.dumps(dict(now=1_000_000_000, hashes=0, dates=0, sleeps=[]))
                )
                wrapper = CLOCK_TOOL.format(
                    python=sys.executable, sha=shutil.which("sha256sum")
                )
                for name in ("date", "sleep", "sha256sum"):
                    tool = directory / name
                    tool.write_text(wrapper)
                    tool.chmod(0o755)

                master, slave = pty.openpty()
                fcntl.ioctl(
                    slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0)
                )

                def terminal():
                    os.setsid()
                    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

                process = subprocess.Popen(
                    [
                        "bash",
                        str(SCRIPT),
                        "--no-wait",
                        "--interval",
                        interval,
                        str(source),
                    ],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    preexec_fn=terminal,
                    env={
                        **os.environ,
                        "CHAPPE_TEST_CLOCK": str(clock),
                        "PATH": f"{directory}:{os.environ['PATH']}",
                    },
                )
                os.close(slave)
                output = bytearray()
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if not select.select([master], [], [], 0.1)[0]:
                            continue
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        output.extend(data)
                    self.assertEqual(process.wait(timeout=2), 0, output.decode())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)

                transcript = output.decode()
                self.assertEqual(transcript.count("\x1b[24;1H"), 10)
                self.assertIn("\x1b[?1049l", transcript)
                state = json.loads(clock.read_text())
                if interval == "0":
                    self.assertEqual(state["dates"], 0)
                    self.assertEqual(state["sleeps"], [])
                    continue
                displays = [
                    int(value)
                    for value in re.findall(r"\x1b\[24;1H█{80}CLOCK:(\d+)", transcript)
                ]
                self.assertEqual(len(displays), 10)
                gaps = [right - left for left, right in zip(displays, displays[1:])]
                self.assertEqual(
                    gaps, [100_000_000] * 4 + [250_000_000] + [100_000_000] * 4
                )
                self.assertIn(70_000_000, state["sleeps"])
                # The final end screen must receive its full hold before cleanup.
                self.assertEqual(state["now"] - displays[-1], 100_000_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
