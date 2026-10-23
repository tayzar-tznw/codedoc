package iryo.reception.flows;

import iryo.billing.FeeCalculator;
import iryo.domain.Money;
import iryo.kernel.PaymentPolicy;
import iryo.kernel.StandardPolicy;
import iryo.kernel.UrgentPolicy;
import iryo.legacy.claims.OldTable;
import java.math.BigDecimal;

import static iryo.billing.PointMath.toYen;

/**
 * 点数計算から支払い受領、領収書発行までの会計処理。
 */
public final class AccountingFlow {
    private AccountingFlow() {
    }

    public static void run() {
        System.out.println("-- 会計 --");
        var fee = new FeeCalculator();

        int basic = fee.calculate(288, 146);
        long wide = fee.calculate(288, 146L);
        double split = fee.calculate(70.5, 2);
        int triple = fee.calculate(288, 146, 52);
        int tapered = fee.calculate(100, 100, 100, 100);
        int exact = fee.calculate(120, 80);
        System.out.println("点数: 基本" + basic + " 広域" + wide + " 按分" + split
                + " 三項" + triple + " 逓減" + tapered + " 完全一致" + exact);

        System.out.println("締め処理: " + fee.run(() -> {
        }));
        System.out.println("締め処理: " + fee.run(() -> "集計完了"));
        System.out.println("請求種別: " + fee.bill(null));
        System.out.println("受付経路: " + fee.route(42));

        int scaled = fee.scale(100, 2);
        double regional = fee.scale(100, 1.08);
        System.out.println("倍率適用: " + scaled + " / 地域加算 " + regional);

        var consultation = new Money(new BigDecimal("1200"));
        var medication = new Money(new BigDecimal("800"));
        var total = consultation.plus(medication);
        BigDecimal received = fee.pay(total.amount());
        System.out.println("請求 " + total + " / 受領 " + received.toPlainString() + "円");

        int yen = toYen(126);
        int rounded = iryo.billing.PointMath.roundTo10(1264);
        int pharmacyYen = iryo.pharmacy.PointMath.toYen(126);
        System.out.println("点数換算: 会計 " + yen + "円 / 丸め " + rounded
                + "円 / 薬局窓口 " + pharmacyYen + "円");

        PaymentPolicy standard = new StandardPolicy();
        PaymentPolicy urgent = new UrgentPolicy();
        var combined = new CombinedPolicy();
        System.out.println("手数料率: 標準 " + standard.fee()
                + " / 時間外 " + urgent.fee()
                + " / 併用 " + combined.fee()
                + " / 名称 " + PaymentPolicy.policyName());

        var current = new iryo.billing.text.ReceiptFormatter();
        var legacy = new iryo.legacy.claims.text.ReceiptFormatter();
        var interop = new iryo.interop.text.ReceiptFormatter();
        System.out.println(current.format(total));
        System.out.println(legacy.format(total));
        System.out.println(interop.format(total));

        int migrated = new OldTable().calculate(30, 10);
        System.out.println("旧点数表突合: " + migrated);
    }

    private interface ReviewPolicy {
        default double fee() {
            return 0.10;
        }
    }

    private static final class CombinedPolicy implements PaymentPolicy, ReviewPolicy {
        @Override
        public double fee() {
            return PaymentPolicy.super.fee();
        }
    }
}
