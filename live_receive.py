"""Frame-by-frame reception shared by the Android app and desktop tests.

Call every method on the same worker thread. The Android capture layer owns
the bounded frame spool; this layer stores verified pages and completed output.
"""

import json
import sqlite3
import tempfile
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from receive import Decoder, Recovery


class LiveReceiver:
    def __init__(self, directory, max_bytes=64 * 1024**2):
        self.directory = tempfile.TemporaryDirectory(prefix="transfer-", dir=directory)
        self.recovery = Recovery(
            str(Path(self.directory.name) / "pages.sqlite"), max_bytes
        )
        # Temporary state is discarded after process death: a rollback journal
        # is unnecessary. Bound SQLite itself, including per-page overhead.
        self.recovery.db.execute("PRAGMA journal_mode=OFF")
        page_size = self.recovery.db.execute("PRAGMA page_size").fetchone()[0]
        self.recovery.db.execute(f"PRAGMA max_page_count={128 * 1024**2 // page_size}")
        self.decoder = Decoder()
        self.output = Path(self.directory.name) / "received.bin"
        self.frames = 0
        self.pages = 0
        self.bytes = 0
        self.frame_times = deque(maxlen=30)
        self.decode_times = deque(maxlen=30)
        self.result = {}
        self.closed = False
        self.error = None

    def accept_frame(self, image):
        if self.closed:
            raise ValueError("Receiver is closed")
        if self.error or self.result.get("status") == "complete":
            return self.status()
        now = time.monotonic()
        if self.frame_times and now - self.frame_times[-1] > 2:
            self.frame_times.clear()
        self.frame_times.append(now)
        self.frames += 1
        try:
            changed = False
            for header, data in self.decoder.frame(image):
                previous_end = self.recovery.end
                before = self.recovery.db.total_changes
                self.recovery.accept(header, data)
                inserted = self.recovery.db.total_changes - before
                self.pages += inserted
                if inserted:
                    self.bytes += len(data)
                changed |= bool(inserted) or self.recovery.end != previous_end
            if changed:
                self.recovery.db.commit()
                if self.recovery.end is not None:
                    self.result = self.recovery.report()
                    if self.result["status"] == "complete":
                        # This destination belongs to this private temporary
                        # directory. Use rename, not no-clobber hard linking:
                        # the latter is denied on some Android filesystems.
                        self.result["output_sha256"] = self.recovery.write(
                            self.output, force=True
                        )
        except (ValueError, OSError, sqlite3.Error, cv2.error) as error:
            # Keep the UI responsive and preserve the failure until an explicit reset.
            self.error = str(error)
        self.decode_times.append((time.monotonic() - now) * 1000)
        return self.status()

    def accept_luma(self, data, width, height, rotation=0):
        """Accept tightly packed Y-plane bytes from the Android camera."""
        if width <= 0 or height <= 0:
            raise ValueError("Invalid camera dimensions")
        frame = np.frombuffer(data, dtype=np.uint8)
        if frame.size != width * height:
            raise ValueError("Unexpected camera buffer size")
        if rotation not in (0, 90, 180, 270):
            raise ValueError("Invalid camera rotation")
        frame = np.rot90(frame.reshape(height, width), -(rotation // 90))
        return json.dumps(self.accept_frame(frame))

    def status(self):
        elapsed = (
            max(self.frame_times[-1] - self.frame_times[0], 0.001)
            if self.frame_times
            else 0.001
        )
        return {
            **self.result,
            "status": "error" if self.error else self.result.get("status", "receiving"),
            "error": self.error,
            "frames": self.frames,
            "detected_panels": self.decoder.panels,
            "decode_ms": round(sum(self.decode_times) / max(len(self.decode_times), 1)),
            "checked_pages": self.pages,
            "checked_bytes": self.bytes,
            "expected_pages": self.recovery.end,
            "frames_per_second": round(max(len(self.frame_times) - 1, 0) / elapsed, 1),
        }

    def capture_directory(self):
        return str(Path(self.directory.name) / "frames")

    def completed_path(self):
        if self.closed or self.error or self.result.get("status") != "complete":
            raise ValueError("Transfer is not complete")
        return str(self.output)

    def close(self):
        if not self.closed:
            self.recovery.db.close()
            self.directory.cleanup()
            self.closed = True
