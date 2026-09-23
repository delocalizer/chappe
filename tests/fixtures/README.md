# Physical recording fixtures

These files are used by the receiver regression tests. Source files are used
only for the final byte-for-byte comparison, never to help decoding.

| Recording | Original bytes | Regression |
|---|---|---|
| `small.mp4` | `small.bin` (6,100 bytes) | Bright spillover into dark subregions |

The physical-recording test skips if its recording or source file is absent.
Keep this pair available when validating a release. A synthetic video generated
from deterministic random bytes tests window decoration merging with the panel
border, without storing another recording or source file.
