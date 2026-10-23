package iryo.interop;

import java.util.Locale;

/**
 * 外部連携用の患者 ID を正規化する。
 */
public final class PatientIdKit {
    private PatientIdKit() {
    }

    public static String normalize(String raw) {
        return raw.trim().toUpperCase(Locale.ROOT).replace(' ', '-');
    }
}
