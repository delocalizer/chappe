# Chappe for Android

A native camera receiver for CB01 transfers. It decodes frames on the phone as
screens arrive; there is no intermediate video recording or network service.

## Install and receive

Install `app/build/outputs/apk/debug/app-debug.apk` after building below. The
initial app supports Android 7.0+ on 64-bit ARM phones and x86-64 emulators.

1. Open Chappe and grant camera access.
2. Frame the whole terminal, including its white border, then tap **Start receiving**.
3. Run `send.sh` as usual. Watch the verified page count while holding the phone steady.
4. When **Transfer complete** appears, tap **Save file**, choose a name and location.

The protocol does not transmit filenames, so choose the appropriate extension
when saving. Saving requires every checksummed page and the end marker. If the
end marker arrives with missing pages, reception continues: replay the **same
input and sender mode** to fill gaps. Use **New transfer** before sending any
different file; CB01 has no transfer identifier.

**Pause** stops capture while queued frames continue decoding. After the sender
finishes, pause and wait for the queue to drain before judging recovery.
Backgrounding suspends capture; returning resumes it if it was running.
Rotation retains the transfer. New transfer, closing the activity, or process
termination loses progress. Save completed files promptly.

## Speed and bounded storage

Version 0.2.0 separates capture from decoding. A dedicated worker writes raw,
lossless grayscale frames to a disk FIFO; the decoder consumes them in order.
It first tries the previous terminal geometry and searches the same frame
again if validation fails. Checksums remain mandatory.

Temporary transfer storage has these limits:

| Component | Limit |
|---|---:|
| Queued camera frames, including the frame being decoded | 512 MiB |
| SQLite page database, including database overhead | 128 MiB |
| Reconstructed file | 64 MiB |

That is at most 704 MiB of transfer file contents, plus filesystem metadata.
The installed runtime and a user-exported copy are separate. The buffer refuses
new frames when less than 256 MiB of free space would remain, reserving room
for database growth and assembly. Frame files are removed after decoding;
completion discards remaining frames. Reset/close removes the session, and
startup removes sessions abandoned by process death. SQLite journaling is
disabled for this disposable database, with its size independently capped.

If the frame buffer fills or free space gets low, **capture pauses visibly**
while decoding continues. Queued frames are not overwritten. Resume after the
queue drains and replay the same transfer to fill any resulting gaps. A finite
buffer cannot accommodate an indefinitely faster sender. At 1920 × 1080 the
buffer holds about 258 frames (8.6 seconds at 30 fps) if decoding stops entirely;
ongoing decoding extends the capture time available.

CameraX uses a small blocking queue ahead of the capture worker. Disk or camera
stalls can still reduce capture rate. This does not guarantee that every sensor
frame is delivered. There is no background capture or video-import interface.
Version 0.2.0 recovered live transfers on an Oppo A54 5G at a 0.2-second
hold in manual testing. Transfers at 0.1 seconds did not complete. These results
are specific to the tested phone, terminal and recording conditions.

## Diagnosing incomplete reception

The status includes the actual camera resolution, recent analyzed frames per
second, mean decoding time, and how many analyzed frames contained a readable
header. Once the end marker is recovered it also lists missing page numbers
(starting at zero). Pause after a failed transfer and let the displayed frame queue drain.

Report these numbers along with the phone model and sender command. Low frame
throughput can indicate that processing misses screens; frequent readable
headers with missing payloads points toward sampling or image quality. These
are clues, not a definitive diagnosis. An unknown total means the end marker
has not been recovered. Diagnostic fields contain no file contents.

## Build

Install JDK 17, Python 3.10, and Android SDK platform 35 plus build tools 35.0.0.
Set `ANDROID_HOME` to the SDK directory, or create `local.properties` containing
`sdk.dir=/absolute/path/to/android-sdk`. Then, from this directory:

```sh
./gradlew assembleDebug testDebugUnitTest lintDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The first build downloads Gradle, Android libraries and Python wheels. The APK
contains its runtime and needs no Python installation on the phone. The debug
APK is for testing; distribution requires your own release signing key.

## Implementation

CameraX supplies the Y plane to the capture worker, respecting row and pixel
strides. A separate worker owns Python and SQLite. The preview and controls stay on the main thread. Chaquopy embeds
Python, NumPy and OpenCV; `../live_receive.py` adapts the shared `../receive.py`
decoder to frames and stores verified pages in SQLite. Only those two Python
source modules are copied into the build, never recordings or test data.

Python 3.10, NumPy 1.26.2 and OpenCV 4.5.1.48 are pinned to available
[Chaquopy Android wheels](https://chaquo.com/pypi-13.1/). Desktop dependency
requirements remain unchanged. The app requests camera permission and has no
Internet or broad storage permission; export uses Android's document picker.

Run shared receiver tests from the repository root:

```sh
python -m unittest discover -s tests -v
```
