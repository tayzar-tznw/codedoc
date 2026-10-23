package iryo.probe.xpr;

import iryo.billing.Validator;
import iryo.billing.PointMath;
import iryo.kernel.logging.Logger;

/**
 * 受付端末 {@code DailyCloseFlow.run()} と同じ手続き行を、受付側の import で束縛する複製。
 *
 * <p>{@link iryo.probe.xpr.AdminSide} とは import 宣言とクラス名だけが異なり、
 * 手続き行は 1 バイトも違わない。観測値が分岐すれば、束縛先を決めているのは
 * 行のテキストではなく各プロジェクトの import であることの実行時証明になる。</p>
 */
public final class ReceptionSide {
    private ReceptionSide() {
    }

    public static String[] run() {
        var ledger = new Logger();
        ledger.log("日次点検");
        var check = new Validator();
        int yen = PointMath.toYen(15);
        return new String[] {ledger.lines().get(0), String.valueOf(check.validate("H12345678")), yen + "円"};
    }
}
