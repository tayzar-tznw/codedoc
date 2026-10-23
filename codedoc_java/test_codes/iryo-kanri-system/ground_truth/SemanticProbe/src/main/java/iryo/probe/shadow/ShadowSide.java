package iryo.probe.shadow;

import static iryo.billing.PointMath.toYen;

/**
 * 受付端末 {@code MaintenanceFlow} と同じ遮蔽構造の複製。
 *
 * <p>製品側の局所 {@code toYen} は private static で、しかも reception-desk は
 * ライブラリとして配布されない(SemanticProbe のクラスパスに載らない)ため、
 * 同じ宣言形をここで再現して JLS 6.4.1 の遮蔽規則を実行時に確認する。</p>
 */
public final class ShadowSide {
    private ShadowSide() {
    }

    /** 非修飾呼び出し: 静的インポートはクラス自身の宣言に覆い隠される (×11)。 */
    public static int shadowed(int points) {
        return toYen(points);
    }

    /** 完全修飾呼び出し: 静的インポート元の会計用換算に束縛される (×10)。 */
    public static int imported(int points) {
        return iryo.billing.PointMath.toYen(points);
    }

    private static int toYen(int points) {
        return points * 11;
    }
}
