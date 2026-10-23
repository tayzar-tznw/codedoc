package iryo.reception.flows;

import iryo.kernel.*;
import iryo.pharmacy.*;
import iryo.pharmacy.Logger;
import java.math.BigDecimal;

/**
 * 処方箋確認と調剤部門への連携を行う。
 */
public final class PharmacyFlow {
    private PharmacyFlow() {
    }

    public static void run() {
        System.out.println("-- 薬局 --");

        var houseLog = new iryo.kernel.logging.Logger("薬局窓口");
        houseLog.log("処方箋受領");
        var dispenseLog = new Logger();
        dispenseLog.log("処方箋受領");
        System.out.println(houseLog.lines().get(0));
        System.out.println(dispenseLog.lines().get(0));

        var rx = new Validator();
        System.out.println("処方箋判定: " + rx.validate("Rp) アモキシシリン 250mg"));
        var insurance = new iryo.billing.Validator();
        System.out.println("保険証判定: " + insurance.validate("H12345678"));

        var dose = new DoseCalculator();
        int mg = dose.calculate(30, 10);
        var pointCalc = new iryo.billing.FeeCalculator();
        int points = pointCalc.calculate(30, 10);
        System.out.println("用量 " + mg + "mg / 点数 " + points + "点");

        var stock = new Money(new BigDecimal("52000"));
        System.out.println("在庫評価額: " + stock.amount.toPlainString() + "円");

        var Pharmacy = Guard.notEmpty("薬局部門", "部門名");
        System.out.println("監査対象: " + Pharmacy);
        var nightLog = new iryo.pharmacy.Logger();
        nightLog.log("夜間在庫確認");
        System.out.println(nightLog.lines().get(0));
    }
}
