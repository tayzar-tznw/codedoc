package iryo.admin;

import iryo.domain.Money;
import java.math.BigDecimal;
import java.util.List;

import static iryo.reporting.KarteKit.heading;

/**
 * 週次の脈拍傾向と収益を集計する。
 */
public final class ReportDigest {
    private ReportDigest() {
    }

    public static void run() {
        System.out.println("-- 週次ダイジェスト --");
        System.out.println(heading("管理集計"));

        int pulseExcess = List.of(70, 88, 64).stream()
                .filter(value -> value > 65)
                .mapToInt(value -> value - 60)
                .sum();
        System.out.println("脈拍超過合計: " + pulseExcess);

        var outpatient = new Money(new BigDecimal("150000"));
        var inpatient = new Money(new BigDecimal("98000"));
        System.out.println("週間収益: " + outpatient.plus(inpatient));
    }
}
