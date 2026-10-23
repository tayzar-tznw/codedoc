package iryo.reception.flows;

import iryo.billing.Validator;
import iryo.billing.PointMath;
import iryo.kernel.logging.Logger;

/**
 * 受付端末の日次締めを行う。
 */
public final class DailyCloseFlow {
    private DailyCloseFlow() {
    }

    public static void run() {
        System.out.println("-- 日次締め --");
        var ledger = new Logger();
        ledger.log("日次点検");
        System.out.println(ledger.lines().get(0));
        var check = new Validator();
        System.out.println("様式判定: " + check.validate("H12345678"));
        int yen = PointMath.toYen(15);
        System.out.println("換算: " + yen + "円");
    }
}
