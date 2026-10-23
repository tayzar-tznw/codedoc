package iryo.pharmacy;

/**
 * 処方箋様式を検証する。
 */
public final class Validator {
    public boolean validate(String prescription) {
        return prescription != null && prescription.contains("Rp");
    }
}
