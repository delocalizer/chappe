package io.github.delocalizer.chappe;

import java.nio.ByteBuffer;

/** Copy only visible luma samples; the last camera row need not include padding. */
final class CameraFrames {
    private CameraFrames() {}

    static byte[] packLuma(ByteBuffer source, int width, int height,
                           int rowStride, int pixelStride) {
        ByteBuffer buffer = source.duplicate();
        byte[] pixels = new byte[width * height];
        int base = buffer.position();
        for (int y = 0; y < height; y++) {
            int row = base + y * rowStride;
            if (pixelStride == 1) {
                buffer.position(row);
                buffer.get(pixels, y * width, width);
            } else {
                for (int x = 0; x < width; x++)
                    pixels[y * width + x] = buffer.get(row + x * pixelStride);
            }
        }
        return pixels;
    }
}
