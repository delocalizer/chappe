# Chappe v1 protocol (CB01)

Coordinates are zero-based. Rows 0 and 23 are full-block borders; columns 0 and
79 are full blocks, and columns 1 and 78 are black margins. The 76 interior
columns contain:

- Rows 1, 2 and 22: identical quadrant headers plus 12 alternating white/black
  cells.
- Row 3: alternating white/black reference cells, starting white.
- Rows 4–21: 1,368 payload cells.

Masks number subregions left-to-right, top-to-bottom, with the upper-left
region at bit 0. Quadrants are a 2×2 grid; sextants a 2×3 grid. Source bytes
are read MSB-first and grouped into 4- or 6-bit integers. Each integer selects
a mask.  Unused bits and cells are zero (black). Sextant masks 0, 21, 42 and 63
use space, left half block, right half block and full block; other masks use
U+1FB00–U+1FB3B.

The 32-byte header is encoded high nibble first as 64 quadrant cells:

| Bytes | Contents | |---|---| | 0–3 | ASCII `CB01` | | 4 | ASCII `C`
(calibration), `D` (data), or `E` (end) | | 5 | Bits per payload cell: 4 or 6 |
| 6–13 | Unsigned big-endian data page index; end uses page count | | 14–15 |
Unsigned big-endian payload byte length | | 16–31 | First 16 SHA-256 bytes of
lowercase ASCII hex of bytes 0–15, followed by source payload bytes |

Calibration uses index and length zero, and cycles through masks in ascending
order across all payload cells. End screens have length zero and a black body.
Both use empty source payloads for the checksum. Data pages start at index
zero.  The final data page may be short; empty input has no data pages. Bash
counters are finite machine integers despite bounded memory use for streaming.

The sender displays four calibration screens, each data page once, and three
end screens. Each display is held for the configured interval. The receiver
must validate each page checksum, require contiguous page indices starting at
zero, and require a checked end marker before writing output. All nonfinal data
pages must have full capacity. No session ID, error correction, or whole-file
digest is carried by CB01.
