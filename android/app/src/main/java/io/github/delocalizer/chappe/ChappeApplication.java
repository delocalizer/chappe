package io.github.delocalizer.chappe;

import android.app.Application;
import java.io.File;

/** Transfers do not survive process death. Remove their bounded spools at startup. */
public final class ChappeApplication extends Application {
    @Override public void onCreate() {
        super.onCreate();
        File[] files = getCacheDir().listFiles();
        if (files != null) {
            for (File file : files) {
                if (file.getName().startsWith("transfer-")) remove(file);
            }
        }
    }

    private static void remove(File file) {
        File[] children = file.listFiles();
        if (children != null) for (File child : children) remove(child);
        if (!file.delete()) throw new IllegalStateException("Cannot clean previous transfer storage");
    }
}
