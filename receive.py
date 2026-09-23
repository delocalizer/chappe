#!/usr/bin/env python3
"""Decode one CB01 block-sender recording. Only write complete, checked output."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import cv2
import numpy as np

SCREEN_ROWS, SCREEN_COLUMNS = 24, 80
BODY_ROWS, BODY_COLUMNS = 18, 76
BODY_CELLS = BODY_ROWS * BODY_COLUMNS
CW, CH = 12, 24
WIDTH, HEIGHT = SCREEN_COLUMNS * CW, SCREEN_ROWS * CH
BRIGHTNESS_THRESHOLDS = (0.5, 0.6, 0.4, 0.7, 0.3, 0.55, 0.45, 0.65, 0.35, 0.75, 0.25)
MAX_PENDING_PAGES = 8
MAX_CHECKED_HEADERS = 32


@dataclass(frozen=True)
class Header:
    raw: bytes
    kind: str
    bits: int
    index: int
    length: int

    @property
    def capacity(self):
        return BODY_CELLS * self.bits // 8

    def checks(self, payload):
        return (
            len(payload) == self.length
            and hashlib.sha256(self.raw[:16].hex().encode() + payload).digest()[:16]
            == self.raw[16:]
        )


def parse_header(raw):
    if (
        len(raw) != 32
        or raw[:4] != b"CB01"
        or raw[4] not in b"CDE"
        or raw[5] not in (4, 6)
    ):
        return None
    h = Header(
        raw,
        chr(raw[4]),
        raw[5],
        int.from_bytes(raw[6:14], "big"),
        int.from_bytes(raw[14:16], "big"),
    )
    if h.kind == "D":
        return h if 0 < h.length <= h.capacity else None
    return h if h.length == 0 and (h.kind != "C" or h.index == 0) else None


def bytes_from_light(light, bits, threshold=0.5):
    # Optical regions are bit 0 first; source symbols were emitted MSB first.
    binary = (light > threshold).reshape(-1, bits)[:, ::-1].reshape(-1)
    return np.packbits(binary).tobytes()


def checked_payload(header, light):
    # Camera blur and glyph edge spill can lift black regions above midpoint.
    # A small, fixed search is safe because the transmitted checksum is required.
    for threshold in BRIGHTNESS_THRESHOLDS:
        payload = bytes_from_light(light, header.bits, threshold)[: header.length]
        if header.checks(payload):
            return payload
    return None


def ordered(points):
    p = np.asarray(points, np.float32).reshape(4, 2)
    center = p.mean(0)
    p = p[np.argsort(np.arctan2(p[:, 1] - center[1], p[:, 0] - center[0]))]
    return np.roll(p, -np.argmin(p.sum(1)), axis=0)


def fit_panel_edges(contour, quad, shape):
    """Intersect visible border lines, excluding artificial image-boundary edges.

    A partly clipped corner may lie outside the frame. Polygon approximation
    alone puts it on the image edge and distorts the entire sampling grid.
    """
    height, width = shape
    points = contour.reshape(-1, 2).astype(np.float32)
    visible = (
        (points[:, 0] > 1)
        & (points[:, 0] < width - 2)
        & (points[:, 1] > 1)
        & (points[:, 1] < height - 2)
    )
    points = points[visible]
    sides = np.roll(quad, -1, axis=0) - quad
    shortest = np.linalg.norm(sides, axis=1).min()
    tolerance = max(3, shortest * 0.035)
    lines = []
    for start, edge in zip(quad, sides):
        length = np.linalg.norm(edge)
        direction = edge / length
        delta = points - start
        along = delta @ direction
        distance = abs(delta[:, 0] * direction[1] - delta[:, 1] * direction[0])
        selected = points[
            (along > length * 0.05) & (along < length * 0.95) & (distance < tolerance)
        ]
        if len(selected) < 8 or np.ptp(selected @ direction) < length * 0.2:
            return None
        vx, vy, x, y = cv2.fitLine(selected, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append(np.array([-vy, vx, vy * x - vx * y]))
    corners = []
    for index in range(4):
        point = np.cross(lines[index - 1], lines[index])
        if abs(point[2]) < 1e-5:
            return None
        corners.append(point[:2] / point[2])
    refined = np.float32(corners)
    if (
        not cv2.isContourConvex(refined)
        or np.max(abs(refined - quad)) > shortest * 0.07
    ):
        return None
    return refined


def find_panels(gray):
    scale = min(1.0, 1200 / max(gray.shape))
    small = cv2.resize(gray, None, fx=scale, fy=scale) if scale < 1 else gray
    cutoff, binary = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates = []
    # Nearby bright window decorations can join the border at Otsu's cutoff,
    # turning its contour into a non-quadrilateral. Brighter cuts separate them.
    for threshold in (None, min(cutoff + 32, 245), min(cutoff + 64, 245)):
        if threshold is not None:
            _, binary = cv2.threshold(small, threshold, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            area = cv2.contourArea(contour)
            if area < 5000 * scale**2:
                break
            perimeter = cv2.arcLength(contour, True)
            poly = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
            if len(poly) != 4 or not cv2.isContourConvex(poly):
                continue
            q = ordered(poly[:, 0, :])
            if np.min(np.linalg.norm(q - np.roll(q, 1, axis=0), axis=1)) < 90 * scale:
                continue
            refined = fit_panel_edges(contour, q, small.shape)
            for candidate in (refined, q):
                if candidate is None:
                    continue
                candidate = candidate / scale
                if not any(np.max(abs(candidate - old)) < 1 for _, old in candidates):
                    candidates.append((area, candidate))
    return [q for _, q in sorted(candidates, key=lambda item: -item[0])[:12]]


def sample_cells(panel, rows, columns, bits):
    cells = panel.reshape(SCREEN_ROWS, CH, SCREEN_COLUMNS, CW).transpose(0, 2, 1, 3)
    cells = cells[np.asarray(rows)[:, None], np.asarray(columns)[None, :]]
    height = CH // (bits // 2)
    regions = []
    for y in range(bits // 2):
        for x in range(2):
            # Central half avoids glyph boundaries and interpolation bleed.
            patch = cells[
                ...,
                y * height + height // 4 : (y + 1) * height - height // 4,
                x * 6 + 2 : x * 6 + 4,
            ]
            regions.append(patch.mean(axis=(-2, -1)))
    return np.stack(regions, axis=-1)


def rectify(gray, quad):
    target = np.float32(
        [[0, 0], [WIDTH - 1, 0], [WIDTH - 1, HEIGHT - 1], [0, HEIGHT - 1]]
    )
    matrix = cv2.getPerspectiveTransform(np.float32(quad), target)
    panel = cv2.warpPerspective(gray, matrix, (WIDTH, HEIGHT)).astype(np.float32)
    references = sample_cells(panel, [3], range(2, 78), 4).mean(axis=-1)[0]
    white, black = np.median(references[::2]), np.median(references[1::2])
    if white - black < 40:
        return None
    # The alternating reference row also rejects the wrong orientation or grid.
    correct = np.r_[
        references[::2] > (white + black) / 2, references[1::2] < (white + black) / 2
    ].mean()
    if correct < 0.88:
        return None
    return np.clip((panel - black) / (white - black), 0, 1)


class Decoder:
    def __init__(self):
        self.previous_quad = None
        self.pending = OrderedDict()
        self.checked = OrderedDict()
        self.panels = 0

    def accumulate(self, header, body):
        """Combine one observation per video frame, with bounded running weight."""
        previous = self.pending.pop(header.raw, None)
        if previous is None:
            mean, count = body.copy(), 1
        else:
            mean, count = previous
            if np.mean((mean > 0.5) != (body > 0.5)) > 0.20:
                mean, count = body.copy(), 1
            else:
                count = min(count + 1, 64)
                mean += (body - mean) / count
        self.pending[header.raw] = (mean, count)
        while len(self.pending) > MAX_PENDING_PAGES:
            self.pending.popitem(last=False)
        return mean

    def frame(self, image):
        observed = set()
        panel_seen = False
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        def candidates():
            # Reuse geometry only while references, headers and payload checks
            # agree. A failed attempt still searches this very same frame.
            if self.previous_quad is not None:
                yield self.previous_quad
            for candidate in find_panels(gray):
                for rotation in range(4):
                    yield np.roll(candidate, rotation, axis=0)

        for quad in candidates():
            panel = rectify(gray, quad)
            if panel is None:
                continue
            light = sample_cells(panel, [1, 2, 22], range(2, 66), 4)
            # Try each copy as well as voting; transitions may mix screens.
            versions = [np.median(light, axis=0), light.mean(axis=0), *light]
            headers = []
            for version in versions:
                h = parse_header(bytes_from_light(version, 4))
                if h is not None and h not in headers:
                    headers.append(h)
            if not headers:
                continue
            self.previous_quad = quad
            if not panel_seen:
                self.panels += 1
                panel_seen = True
            results = []
            for h in headers:
                if h.raw in self.checked:
                    continue
                if h.kind != "D":
                    if h.checks(b""):
                        results.append((h, b""))
                    continue
                body = sample_cells(panel, range(4, 22), range(2, 78), h.bits)
                payload = checked_payload(h, body)
                if payload is None and h.raw not in observed:
                    observed.add(h.raw)
                    payload = checked_payload(h, self.accumulate(h, body))
                if payload is not None:
                    results.append((h, payload))
            for h, _ in results:
                self.checked[h.raw] = True
                self.pending.pop(h.raw, None)
            while len(self.checked) > MAX_CHECKED_HEADERS:
                self.checked.popitem(last=False)
            if results:
                return results
            if all(h.raw in self.checked for h in headers):
                return []
        return []


class Recovery:
    def __init__(self, path, max_bytes):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE pages (idx INTEGER PRIMARY KEY, data BLOB)")
        self.bits = None
        self.end = None
        self.max_bytes = max_bytes
        self.calibrations = 0

    def accept(self, h, data):
        if not h.checks(data):
            raise ValueError("Unchecked packet")
        if self.bits is not None and h.bits != self.bits:
            raise ValueError("Recording contains conflicting encoding modes")
        self.bits = h.bits
        # SQLite indices are signed 64-bit integers, unlike the wire format.
        if h.index > 2**63 - 1:
            raise ValueError("Page index exceeds storage limits")
        minimum_size = (
            (h.index - 1) * h.capacity + 1
            if h.kind == "E" and h.index
            else h.index * h.capacity + h.length
        )
        if minimum_size > self.max_bytes:
            raise ValueError("Page index exceeds --max-bytes")
        if h.kind == "C":
            self.calibrations += 1
        elif h.kind == "E":
            if self.end is not None and self.end != h.index:
                raise ValueError("Conflicting end page counts")
            self.end = h.index
        else:
            old = self.db.execute(
                "SELECT data FROM pages WHERE idx=?", (h.index,)
            ).fetchone()
            if old and old[0] != data:
                raise ValueError(
                    "Conflicting checked pages; use one transfer per video"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO pages VALUES (?, ?)", (h.index, data)
            )

    def report(self):
        missing, short = [], []
        next_index = 0
        size = 0
        capacity = BODY_CELLS * self.bits // 8 if self.bits else 0
        for index, length in self.db.execute(
            "SELECT idx,length(data) FROM pages ORDER BY idx"
        ):
            if self.end is not None and index >= self.end:
                raise ValueError("Data page index exceeds end page count")
            if index > next_index:
                missing.append([next_index, index - 1])
            if self.end is not None and index < self.end - 1 and length != capacity:
                short.append(index)
            next_index = index + 1
            size += length
        if self.end is not None and next_index < self.end:
            missing.append([next_index, self.end - 1])
        count = self.db.execute("SELECT count(*) FROM pages").fetchone()[0]
        complete = (
            self.end is not None and not missing and not short and count == self.end
        )
        return dict(
            status="complete" if complete else "incomplete",
            bits_per_cell=self.bits,
            checked_pages=count,
            expected_pages=self.end,
            checked_bytes=size,
            missing_page_ranges=missing,
            short_nonfinal_pages=short,
            end_seen=self.end is not None,
            calibration_seen=bool(self.calibrations),
        )

    def write(self, output, force):
        if self.report()["status"] != "complete":
            raise ValueError("Cannot write an incomplete transfer")
        fd, name = tempfile.mkstemp(
            prefix="." + output.name + "-", dir=output.absolute().parent
        )
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as stream:
                for (data,) in self.db.execute("SELECT data FROM pages ORDER BY idx"):
                    stream.write(data)
                    digest.update(data)
            if force:
                os.replace(name, output)
            else:
                os.link(name, output)  # Atomic no-clobber publication.
            return digest.hexdigest()
        finally:
            if os.path.exists(name):
                os.unlink(name)


def validate_paths(paths):
    """Reject aliases, including distinct names for the same hard-linked file."""
    for left, right in combinations((path for path in paths if path is not None), 2):
        if left.resolve() == right.resolve() or (
            left.exists() and right.exists() and left.samefile(right)
        ):
            raise ValueError("Video, output and report must be different files")


def write_report(path, report):
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", dir=path.absolute().parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def print_result(report, output):
    if report["status"] == "complete":
        print(
            f"Wrote {report['checked_bytes']} checked bytes to {output}",
            file=sys.stderr,
        )
        print(f"SHA-256: {report['output_sha256']}", file=sys.stderr)
        return
    expected = report["expected_pages"]
    total = "unknown" if expected is None else str(expected)
    print(
        f"Incomplete: {report['checked_pages']}/{total} pages recovered; "
        "output unchanged.",
        file=sys.stderr,
    )
    if report["missing_page_ranges"]:
        ranges = [
            str(first) if first == last else f"{first}-{last}"
            for first, last in report["missing_page_ranges"][:20]
        ]
        if len(report["missing_page_ranges"]) > 20:
            ranges.append("… (see --report for all ranges)")
        print(f"Missing pages: {', '.join(ranges)}", file=sys.stderr)
    if report["short_nonfinal_pages"]:
        print(
            "Some nonfinal pages have invalid lengths; see --report for details.",
            file=sys.stderr,
        )
    if not report["end_seen"]:
        print("End marker not recovered; total page count is unknown.", file=sys.stderr)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        help="Write JSON diagnostics, including missing page ranges",
    )
    parser.add_argument("--crop", type=int, nargs=4, metavar=("X", "Y", "W", "H"))
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing output only on complete recovery",
    )
    parser.add_argument("--max-bytes", type=int, default=8 * 1024**3)
    args = parser.parse_args()
    if args.max_bytes < 0:
        parser.error("--max-bytes must be nonnegative")
    try:
        validate_paths((args.video, args.output, args.report))
    except ValueError as error:
        parser.error(str(error))
    if os.path.lexists(args.output) and not args.force:
        parser.error("Output exists; use --force to replace it")
    if args.crop and (min(args.crop[:2]) < 0 or min(args.crop[2:]) <= 0):
        parser.error("--crop requires nonnegative coordinates and positive dimensions")
    return args


def main():
    args = parse_args()
    video = cv2.VideoCapture(str(args.video))
    if not video.isOpened():
        raise ValueError(f"Cannot open video: {args.video}")
    try:
        with tempfile.TemporaryDirectory(prefix="cb01-receive-") as tmp:
            recovery = Recovery(str(Path(tmp) / "pages.sqlite"), args.max_bytes)
            decoder, frames = Decoder(), 0
            try:
                while True:
                    ok, frame = video.read()
                    if not ok:
                        break
                    frames += 1
                    if args.crop:
                        x, y, w, h = args.crop
                        if (
                            min(x, y) < 0
                            or min(w, h) <= 0
                            or x + w > frame.shape[1]
                            or y + h > frame.shape[0]
                        ):
                            raise ValueError(
                                "Crop is outside decoded video coordinates"
                            )
                        frame = frame[y : y + h, x : x + w]
                    for header, payload in decoder.frame(frame):
                        recovery.accept(header, payload)
                    if frames % 100 == 0:
                        recovery.db.commit()
                        count = recovery.db.execute(
                            "SELECT count(*) FROM pages"
                        ).fetchone()[0]
                        print(
                            f"{frames} frames; {count} checked pages",
                            file=sys.stderr,
                            flush=True,
                        )
                report = recovery.report()
                report.update(frames=frames, detected_panels=decoder.panels)
                if report["status"] == "complete":
                    report["output_sha256"] = recovery.write(args.output, args.force)
                if args.report:
                    write_report(args.report, report)
                print_result(report, args.output)
                return 0 if report["status"] == "complete" else 2
            finally:
                recovery.db.close()
    finally:
        video.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, sqlite3.Error, cv2.error) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
