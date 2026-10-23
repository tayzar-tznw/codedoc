package iryo.probe.xpr;

import iryo.pharmacy.Logger;
import iryo.pharmacy.Validator;
import iryo.pharmacy.PointMath;

/**
 * 管理コンソール {@code DailyBatch.run()} と同じ手続き行を、管理側の import で束縛する複製。
 *
 * <p>{@link iryo.probe.xpr.ReceptionSide} とは import 宣言とクラス名だけが異なり、
 * 手続き行は 1 バイトも違わない。観測値が分岐すれば、束縛先を決めているのは
 * 行のテキストではなく各プロジェクトの import であることの実行時証明になる。</p>
 */
public final class AdminSide {
    private AdminSide() {
    }

    public static String[] run() {
        var ledger = new Logger();
        ledger.log("日次点検");
        var check = new Validator();
        int yen = PointMath.toYen(15);
        return new String[] {ledger.lines().get(0), String.valueOf(check.validate("H12345678")), yen + "円"};
    }
}
