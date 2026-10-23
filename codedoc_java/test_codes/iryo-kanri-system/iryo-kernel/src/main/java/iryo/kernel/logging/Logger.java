package iryo.kernel.logging;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 診療業務共通のロガー。
 */
public class Logger {
    private final String owner;
    private final List<String> recordedLines = new ArrayList<>();

    public Logger() {
        this("既定");
    }

    public Logger(String owner) {
        this.owner = owner;
    }

    public void log(String message) {
        recordedLines.add("[記録] " + message);
    }

    public void log(Exception error) {
        recordedLines.add("[例外] " + error.getMessage());
    }

    public void log(String message, int severity) {
        recordedLines.add("[重要度" + severity + "] " + message);
    }

    public List<String> lines() {
        return Collections.unmodifiableList(recordedLines);
    }

    public String owner() {
        return owner;
    }

    public void flush() {
        // 現在はメモリ上の記録だけを扱う。永続化先は運用環境で差し替える。
    }
}
