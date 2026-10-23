package iryo.pharmacy;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 調剤台帳専用のロガー。
 */
public class Logger {
    private final List<String> recordedLines = new ArrayList<>();

    public void log(String message) {
        recordedLines.add("[調剤] " + message);
    }

    public List<String> lines() {
        return Collections.unmodifiableList(recordedLines);
    }
}
