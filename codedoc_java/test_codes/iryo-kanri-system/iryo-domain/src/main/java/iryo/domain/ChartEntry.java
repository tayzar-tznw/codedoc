package iryo.domain;

import java.time.LocalDateTime;

/**
 * カルテ記載一件。
 */
public class ChartEntry {
    private final int patientId;
    private final String text;
    private final LocalDateTime writtenAt;

    public ChartEntry() {
        this(0, "新規記載");
    }

    public ChartEntry(int patientId, String text) {
        this.patientId = patientId;
        this.text = text;
        this.writtenAt = LocalDateTime.now();
    }

    public int patientId() {
        return patientId;
    }

    public String text() {
        return text;
    }

    public LocalDateTime writtenAt() {
        return writtenAt;
    }
}
