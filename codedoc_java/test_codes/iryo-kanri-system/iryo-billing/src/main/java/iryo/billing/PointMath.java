package iryo.billing;

/**
 * 診療報酬点数を円へ換算するユーティリティ。
 */
public final class PointMath {
    private PointMath() {
    }

    public static int toYen(int points) {
        return points * 10;
    }

    public static int roundTo10(int yen) {
        return (yen + 5) / 10 * 10;
    }
}
