package iryo.admin;

import iryo.pharmacy.Logger;
import iryo.pharmacy.Validator;
import iryo.pharmacy.PointMath;

/**
 * 調剤部門を含む管理者向けの日次バッチ。
 */
public final class DailyBatch {
    private DailyBatch() {
    }

    public static void run() {
        System.out.println("-- 日次バッチ --");
        var ledger = new Logger();
        ledger.log("日次点検");
        System.out.println(ledger.lines().get(0));
        var check = new Validator();
        System.out.println("様式判定: " + check.validate("H12345678"));
        int yen = PointMath.toYen(15);
        System.out.println("換算: " + yen + "円");
    }
}
