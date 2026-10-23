package iryo.kernel;

/**
 * 支払い時の事務手数料方針。
 */
public interface PaymentPolicy {
    default double fee() {
        return 0.05;
    }

    static String policyName() {
        return "既定方針";
    }
}
