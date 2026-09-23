package io.github.delocalizer.chappe;

import org.junit.Test;
import java.nio.ByteBuffer;
import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

public final class CameraFramesTest {
    @Test public void paddedRowsWithOffsetAndShortLastRow() {
        ByteBuffer buffer = ByteBuffer.wrap(new byte[]{99, 1, 2, 0, 0, 3, (byte) 255});
        buffer.position(1);
        assertArrayEquals(new byte[]{1, 2, 3, (byte) 255},
                CameraFrames.packLuma(buffer, 2, 2, 4, 1));
        assertEquals(1, buffer.position());
    }

    @Test public void interleavedSamplesInReadOnlyBuffer() {
        ByteBuffer buffer = ByteBuffer.wrap(new byte[]{1, 99, 2, 99, 0, 3, 99, 4})
                .asReadOnlyBuffer();
        assertArrayEquals(new byte[]{1, 2, 3, 4},
                CameraFrames.packLuma(buffer, 2, 2, 5, 2));
    }
}
