package io.github.delocalizer.chappe;

import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import java.io.File;
import java.nio.file.Files;
import java.util.concurrent.Executors;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;
import static org.junit.Assert.*;

public final class FrameBufferTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder();

    @Test public void fullBufferPreservesFramesAndReclaimsOnlyAfterDecode() throws Exception {
        File directory = new File(temporary.getRoot(), "frames");
        try (FrameBuffer buffer = new FrameBuffer(directory, 8, 0)) {
            assertTrue(buffer.offer(new byte[]{1, 2, 3, 4}, 2, 2, 90));
            assertTrue(buffer.offer(new byte[]{5, 6, 7, 8}, 2, 2, 0));
            FrameBuffer.Frame first = buffer.poll();
            assertFalse(buffer.offer(new byte[4], 2, 2, 0));
            assertEquals(8, buffer.bytes());
            assertArrayEquals(new byte[]{1, 2, 3, 4}, Files.readAllBytes(first.file.toPath()));
            assertEquals(90, first.rotation);
            buffer.release(first);
            assertTrue(buffer.offer(new byte[]{9, 10, 11, 12}, 2, 2, 0));
            FrameBuffer.Frame second = buffer.poll();
            assertArrayEquals(new byte[]{5, 6, 7, 8}, Files.readAllBytes(second.file.toPath()));
            buffer.release(second);
        }
        assertFalse(directory.exists());
    }

    @Test public void insufficientSpaceAndOversizedFrameDoNotCreateFiles() throws Exception {
        File directory = new File(temporary.getRoot(), "frames");
        try (FrameBuffer buffer = new FrameBuffer(directory, 8, 0)) {
            assertFalse(buffer.offer(new byte[9], 3, 3, 0));
            assertEquals(0, directory.list().length);
        }
        try (FrameBuffer buffer = new FrameBuffer(directory, 8, Long.MAX_VALUE - 8)) {
            assertFalse(buffer.offer(new byte[4], 2, 2, 0));
            assertEquals(0, buffer.pending());
        }
    }

    @Test public void producerAndConsumerKeepOrderAcrossRepeatedRefills() throws Exception {
        File directory = new File(temporary.getRoot(), "frames");
        ExecutorService producer = Executors.newSingleThreadExecutor();
        try (FrameBuffer buffer = new FrameBuffer(directory, 32, 0)) {
            Future<?> produced = producer.submit(() -> {
                try {
                    for (int i = 0; i < 100; i++) {
                        while (!buffer.offer(new byte[]{(byte) i}, 1, 1, 0)) Thread.yield();
                    }
                } catch (Exception error) { throw new RuntimeException(error); }
            });
            for (int i = 0; i < 100; i++) {
                FrameBuffer.Frame frame;
                while ((frame = buffer.poll()) == null) Thread.yield();
                assertEquals((byte) i, Files.readAllBytes(frame.file.toPath())[0]);
                assertTrue(buffer.bytes() <= 32);
                buffer.release(frame);
            }
            produced.get();
            buffer.close();
            assertFalse(buffer.offer(new byte[1], 1, 1, 0));
        } finally { producer.shutdownNow(); }
    }
}
