package io.github.delocalizer.chappe;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.util.ArrayDeque;

/** Bounded disk FIFO. Accounting includes the frame currently being decoded. */
final class FrameBuffer implements AutoCloseable {
    static final long LIMIT = 512L * 1024 * 1024;
    static final long RESERVE = 256L * 1024 * 1024;
    static final class Frame {
        final File file;
        final int width, height, rotation, size;
        Frame(File file, int width, int height, int rotation, int size) {
            this.file = file; this.width = width; this.height = height;
            this.rotation = rotation; this.size = size;
        }
    }
    private final File directory;
    private final long limit, reserve;
    private final ArrayDeque<Frame> queue = new ArrayDeque<>();
    private long bytes;
    private boolean closed;

    FrameBuffer(File directory, long limit, long reserve) throws IOException {
        this.directory = directory;
        this.limit = limit;
        this.reserve = reserve;
        if (!directory.mkdirs() && !directory.isDirectory())
            throw new IOException("Cannot create frame buffer");
    }

    synchronized boolean offer(byte[] data, int width, int height, int rotation) throws IOException {
        if (closed) return false;
        if (data.length > limit - bytes || directory.getUsableSpace() < reserve + data.length)
            return false;
        File file = File.createTempFile("frame-", ".y", directory);
        try (FileOutputStream output = new FileOutputStream(file)) {
            output.write(data);
        } catch (IOException error) {
            file.delete();
            throw error;
        }
        queue.addLast(new Frame(file, width, height, rotation, data.length));
        bytes += data.length;
        return true;
    }

    synchronized Frame poll() { return queue.pollFirst(); }
    synchronized boolean isClosed() { return closed; }
    synchronized int pending() { return queue.size(); }
    synchronized long bytes() { return bytes; }

    synchronized void release(Frame frame) throws IOException {
        if (frame.file.exists() && !frame.file.delete())
            throw new IOException("Cannot remove decoded frame");
        bytes -= frame.size;
    }

    @Override public synchronized void close() throws IOException {
        closed = true;
        while (!queue.isEmpty()) release(queue.removeFirst());
        if (bytes == 0 && directory.exists() && !directory.delete())
            throw new IOException("Cannot remove frame buffer directory");
    }
}
