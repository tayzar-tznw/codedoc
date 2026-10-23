package iryo.legacy.claims;

/**
 * 経過措置加算を含む旧点数表。
 */
@Deprecated(since = "1.0", forRemoval = false)
public class PointTable {
    public int calculate(int a, int b) {
        return a + b + 1000;
    }
}
