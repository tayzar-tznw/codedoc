package iryo.kernel;

/**
 * 時間外・急患向けの支払い方針。
 */
public final class UrgentPolicy implements PaymentPolicy {
    @Override
    public double fee() {
        return 0.25;
    }
}
