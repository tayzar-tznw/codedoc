package iryo.scheduling;

import iryo.domain.Appointment;
import java.util.ArrayList;
import java.util.List;

/**
 * 確定済み予約を保持する台帳。
 */
public final class AppointmentBook {
    private final List<Appointment> appointments = new ArrayList<>();

    public Appointment reserve(Appointment appointment) {
        appointments.add(appointment);
        return appointment;
    }

    public int countFor(int patientId) {
        int count = 0;
        for (Appointment appointment : appointments) {
            if (appointment.patientId() == patientId) {
                count++;
            }
        }
        return count;
    }
}
