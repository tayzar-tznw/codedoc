package iryo.pharmacy;

/**
 * 薬価点数を院内レートで円へ換算する。
 */
public final class PointMath {
    private PointMath() {
    }

    public static int toYen(int points) {
        return (int) Math.ceil(points * 10.8);
    }
}
