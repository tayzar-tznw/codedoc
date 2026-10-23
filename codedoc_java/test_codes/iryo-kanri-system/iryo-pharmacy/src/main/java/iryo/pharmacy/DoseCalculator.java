package iryo.pharmacy;

/**
 * 体重と単位用量から一回量を算出する。
 */
public final class DoseCalculator {
    public int calculate(int weightKg, int mgPerKg) {
        return weightKg * mgPerKg;
    }
}
