package iryo.billing;

/**
 * 保険証記号番号を検証する。
 */
public final class Validator {
    public boolean validate(String insuranceCard) {
        return insuranceCard != null && insuranceCard.startsWith("H");
    }
}
