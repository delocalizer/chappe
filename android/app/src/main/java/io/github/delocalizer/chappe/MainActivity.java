package io.github.delocalizer.chappe;

import android.Manifest;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.util.Size;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.activity.ComponentActivity;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.ImageAnalysis;
import androidx.camera.core.ImageProxy;
import androidx.camera.core.Preview;
import androidx.camera.core.resolutionselector.ResolutionSelector;
import androidx.camera.core.resolutionselector.ResolutionStrategy;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import androidx.core.content.ContextCompat;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import com.google.common.util.concurrent.ListenableFuture;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/** Camera and UI live on Android; the shared Python receiver owns transfer state. */
public final class MainActivity extends ComponentActivity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final ExecutorService captureWorker = Executors.newSingleThreadExecutor();
    private final AtomicBoolean draining = new AtomicBoolean();
    private volatile FrameBuffer frameBuffer;
    private volatile boolean destroyed;
    private volatile String captureWarning;
    private volatile boolean receiving;
    private volatile boolean foreground;
    private PyObject receiver;
    private PreviewView preview;
    private TextView status;
    private Button toggle, reset, save;
    private volatile int generation;
    private ImageAnalysis analysis;
    private boolean cameraReady;
    private boolean decoderReady;

    private final ActivityResultLauncher<String> permission = registerForActivityResult(
            new ActivityResultContracts.RequestPermission(), granted -> {
                if (granted) bindCamera();
                else status.setText(R.string.permission_needed);
            });
    private final ActivityResultLauncher<String> createFile = registerForActivityResult(
            new ActivityResultContracts.CreateDocument("application/octet-stream"), this::saveFile);

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (16 * getResources().getDisplayMetrics().density);
        layout.setPadding(padding, padding, padding, padding);
        // Respect system bars on Android 15, where edge-to-edge is enforced.
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(layout, (view, insets) -> {
            androidx.core.graphics.Insets bars = insets.getInsets(
                    androidx.core.view.WindowInsetsCompat.Type.systemBars());
            view.setPadding(padding + bars.left, padding + bars.top,
                    padding + bars.right, padding + bars.bottom);
            return insets;
        });
        TextView title = new TextView(this);
        title.setText(R.string.app_name);
        title.setTextSize(26);
        layout.addView(title);
        TextView hint = new TextView(this);
        hint.setText(R.string.camera_hint);
        layout.addView(hint);
        preview = new PreviewView(this);
        preview.setScaleType(PreviewView.ScaleType.FIT_CENTER);
        layout.addView(preview, new LinearLayout.LayoutParams(-1, 0, 1));
        status = new TextView(this);
        status.setText(R.string.preparing);
        layout.addView(status);
        LinearLayout actions = new LinearLayout(this);
        layout.addView(actions);
        toggle = button(actions, R.string.start, () -> {
            receiving = !receiving;
            toggle.setText(receiving ? R.string.pause : R.string.resume);
        });
        reset = button(actions, R.string.new_transfer, this::newTransfer);
        save = button(actions, R.string.save, () -> {
            reset.setEnabled(false);
            save.setEnabled(false);
            createFile.launch("received.bin");
        });
        setContentView(layout);
        newTransfer();
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED) bindCamera();
        else permission.launch(Manifest.permission.CAMERA);
    }

    private Button button(LinearLayout parent, int label, Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setOnClickListener(view -> action.run());
        parent.addView(button, new LinearLayout.LayoutParams(0, -2, 1));
        return button;
    }

    private void newTransfer() {
        receiving = false;
        toggle.setEnabled(false);
        reset.setEnabled(false);
        save.setEnabled(false);
        generation++;
        decoderReady = false;
        captureWarning = null;
        worker.execute(() -> {
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
                if (frameBuffer != null) frameBuffer.close();
                if (receiver != null) receiver.callAttr("close");
                receiver = Python.getInstance().getModule("live_receive")
                        .callAttr("LiveReceiver", getCacheDir().getAbsolutePath());
                // All session files share the receiver's unique temporary directory.
                frameBuffer = new FrameBuffer(new File(receiver.callAttr("capture_directory").toString()),
                        FrameBuffer.LIMIT, FrameBuffer.RESERVE);
                runOnUiThread(() -> {
                    decoderReady = true;
                    if (cameraReady) status.setText(R.string.ready);
                    toggle.setText(R.string.start);
                    toggle.setEnabled(cameraReady);
                    reset.setEnabled(true);
                });
            } catch (Exception error) { showError(error); }
        });
    }

    private void bindCamera() {
        ListenableFuture<ProcessCameraProvider> future = ProcessCameraProvider.getInstance(this);
        future.addListener(() -> {
            try {
                ProcessCameraProvider provider = future.get();
                Preview cameraPreview = new Preview.Builder().build();
                cameraPreview.setSurfaceProvider(preview.getSurfaceProvider());
                analysis = new ImageAnalysis.Builder()
                        .setResolutionSelector(new ResolutionSelector.Builder()
                                .setResolutionStrategy(new ResolutionStrategy(new Size(1920, 1080),
                                        ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER)).build())
                        .setBackpressureStrategy(ImageAnalysis.STRATEGY_BLOCK_PRODUCER)
                        .setImageQueueDepth(4).build();
                analysis.setAnalyzer(captureWorker, this::analyze);
                provider.unbindAll();
                provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, cameraPreview, analysis);
                cameraReady = true;
                if (decoderReady) {
                    status.setText(R.string.ready);
                    toggle.setEnabled(true);
                }
            } catch (Exception error) { showError(error); }
        }, ContextCompat.getMainExecutor(this));
    }

    private void analyze(ImageProxy image) {
        int currentGeneration = generation;
        FrameBuffer buffer = frameBuffer;
        try {
            if (!receiving || !foreground || buffer == null) return;
            ImageProxy.PlaneProxy plane = image.getPlanes()[0];
            int width = image.getWidth(), height = image.getHeight();
            byte[] pixels = CameraFrames.packLuma(plane.getBuffer(), width, height,
                    plane.getRowStride(), plane.getPixelStride());
            if (!buffer.offer(pixels, width, height, image.getImageInfo().getRotationDegrees())) {
                if (generation == currentGeneration && !buffer.isClosed()) {
                    receiving = false;
                    captureWarning = getString(R.string.buffer_full);
                    runOnUiThread(() -> {
                        if (generation == currentGeneration) {
                            toggle.setText(R.string.resume);
                            status.setText(captureWarning);
                        }
                    });
                }
            }
            scheduleDrain();
        } catch (Exception error) {
            if (generation == currentGeneration) showError(error);
        } finally { image.close(); }
    }

    private void scheduleDrain() {
        if (!destroyed && draining.compareAndSet(false, true)) worker.execute(this::drainOne);
    }

    private void drainOne() {
        FrameBuffer buffer = frameBuffer;
        int currentGeneration = generation;
        try {
            FrameBuffer.Frame frame = buffer == null ? null : buffer.poll();
            if (frame == null) return;
            JSONObject report;
            try {
                byte[] pixels = new byte[frame.size];
                try (DataInputStream input = new DataInputStream(new FileInputStream(frame.file))) {
                    input.readFully(pixels);
                }
                report = new JSONObject(receiver.callAttr("accept_luma", pixels,
                        frame.width, frame.height, frame.rotation).toString());
                report.put("camera_width", frame.width);
                report.put("camera_height", frame.height);
            } finally { buffer.release(frame); }
            boolean finished = report.optString("status").equals("complete")
                    || report.optString("status").equals("error");
            if (finished) {
                receiving = false;
                buffer.close();
            }
            report.put("queued_frames", buffer.pending());
            report.put("buffer_bytes", buffer.bytes());
            runOnUiThread(() -> {
                if (generation == currentGeneration) updateStatus(report);
            });
        } catch (Exception error) {
            receiving = false;
            try { if (buffer != null) buffer.close(); }
            catch (IOException cleanupError) { error.addSuppressed(cleanupError); }
            if (generation == currentGeneration) showError(error);
        } finally {
            draining.set(false);
            FrameBuffer current = frameBuffer;
            if (!destroyed && current != null && current.pending() > 0) scheduleDrain();
        }
    }

    private void updateStatus(JSONObject report) {
        if (isDestroyed()) return;
        String state = report.optString("status");
        if (state.equals("error")) {
            showError(new IllegalStateException(report.optString("error")));
            return;
        }
        boolean complete = state.equals("complete");
        String expected = report.isNull("expected_pages") ? "?" : report.optString("expected_pages");
        status.setText(getString(R.string.progress,
                getString(complete ? R.string.complete : receiving ? R.string.receiving
                        : report.optInt("queued_frames") > 0 ? R.string.processing : R.string.paused),
                report.optInt("checked_pages"), expected, report.optLong("checked_bytes"),
                report.optDouble("frames_per_second")));
        status.append(getString(R.string.diagnostics,
                report.optInt("camera_width"), report.optInt("camera_height"),
                report.optInt("decode_ms"), report.optInt("detected_panels"), report.optInt("frames")));
        status.append(getString(R.string.buffer_progress, report.optInt("queued_frames"),
                report.optLong("buffer_bytes") / (1024.0 * 1024)));
        if (captureWarning != null && !complete) status.append("\n" + captureWarning);
        if (report.has("missing_page_ranges")) {
            org.json.JSONArray ranges = report.optJSONArray("missing_page_ranges");
            if (ranges != null && ranges.length() > 0) {
                StringBuilder missing = new StringBuilder();
                for (int i = 0; i < Math.min(ranges.length(), 8); i++) {
                    org.json.JSONArray range = ranges.optJSONArray(i);
                    if (i > 0) missing.append(", ");
                    missing.append(range.optLong(0));
                    if (range.optLong(0) != range.optLong(1))
                        missing.append("–").append(range.optLong(1));
                }
                if (ranges.length() > 8) missing.append("…");
                status.append(getString(R.string.missing_pages, missing));
            }
        }
        if (complete) {
            receiving = false;
            toggle.setEnabled(false);
            save.setEnabled(true);
        }
    }

    private void saveFile(Uri uri) {
        if (uri == null) {
            reset.setEnabled(true);
            save.setEnabled(true);
            return;
        }
        reset.setEnabled(false);
        save.setEnabled(false);
        worker.execute(() -> {
            try {
                String path = receiver.callAttr("completed_path").toString();
                try (FileInputStream input = new FileInputStream(path);
                     OutputStream output = getContentResolver().openOutputStream(uri, "wt")) {
                    if (output == null) throw new IllegalStateException("Cannot open destination");
                    byte[] buffer = new byte[65536];
                    int count;
                    while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                }
                runOnUiThread(() -> status.setText(R.string.saved));
            } catch (Exception error) {
                runOnUiThread(() -> status.setText(getString(R.string.save_failed, error.getMessage())));
            } finally {
                runOnUiThread(() -> { reset.setEnabled(true); save.setEnabled(true); });
            }
        });
    }

    private void showError(Exception error) {
        receiving = false;
        runOnUiThread(() -> {
            status.setText(getString(R.string.receive_failed, error.getMessage()));
            toggle.setEnabled(false);
            save.setEnabled(false);
            reset.setEnabled(true);
        });
    }

    @Override protected void onStart() { super.onStart(); foreground = true; }
    @Override protected void onStop() { foreground = false; super.onStop(); }
    @Override protected void onDestroy() {
        receiving = false;
        destroyed = true;
        if (analysis != null) analysis.clearAnalyzer();
        captureWorker.shutdown();
        worker.execute(() -> {
            try { if (frameBuffer != null) frameBuffer.close(); }
            catch (IOException ignored) { /* Receiver cleanup removes the whole session. */ }
            finally { if (receiver != null) receiver.callAttr("close"); }
        });
        worker.shutdown();
        super.onDestroy();
    }
}
