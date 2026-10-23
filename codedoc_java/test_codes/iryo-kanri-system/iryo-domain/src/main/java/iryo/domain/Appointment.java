package iryo.domain;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;

/**
 * 診察予約。
 */
public final class Appointment {
    private static final DateTimeFormatter DISPLAY =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");

    private final int id;
    private final int patientId;
    private final LocalDateTime slot;

    public Appointment(int id, int patientId, LocalDateTime slot) {
        this.id = id;
        this.patientId = patientId;
        this.slot = slot;
    }

    public int id() {
        return id;
    }

    public int patientId() {
        return patientId;
    }

    public LocalDateTime slot() {
        return slot;
    }

    public String summary() {
        return "予約" + id + " (患者" + patientId + " / " + DISPLAY.format(slot) + ")";
    }
}
