package iryo.reception.flows;

import iryo.interop.CsvExporter;
import iryo.interop.Exporter;
import iryo.interop.PatientIdKit;
import iryo.interop.XmlExporter;
import iryo.kernel.logging.Logger;
import iryo.reporting.VitalSequence;
import java.awt.*;
import java.util.List;
import java.util.function.Supplier;

import static iryo.reporting.KarteKit.heading;

/**
 * バイタル集計と外部提出データの作成を行う。
 */
public final class ReportFlow {
    private ReportFlow() {
    }

    public static void run() {
        System.out.println("-- 統計 --");
        System.out.println(heading("日報"));

        var vitals = new VitalSequence(118, 132, 145, 121);
        int overload = vitals.filter(value -> value > 130)
                .map(value -> value - 130)
                .total();
        System.out.println("収縮期超過合計: " + overload);

        int severeTotal = List.of(180, 90, 200).stream()
                .filter(value -> value > 150)
                .mapToInt(Integer::intValue)
                .sum();
        Color alertColor = Color.RED;
        System.out.println("重症域合計: " + severeTotal + " / 警告色R=" + alertColor.getRed());

        var logger = new Logger("統計");
        logger.log(new IllegalStateException("集計対象外データ"));
        logger.log("要確認データ", 2);
        Runnable flushJob = logger::flush;
        flushJob.run();
        Supplier<Logger> loggerFactory = Logger::new;
        System.out.println("生成ロガー: " + loggerFactory.get().owner());

        var north = new iryo.domain.north.Ward();
        var south = new iryo.domain.south.Ward();
        System.out.println("病棟案内: " + north.locate() + " / " + south.locate());

        List<Exporter> exporters = List.of(new CsvExporter(), new XmlExporter());
        for (Exporter exporter : exporters) {
            System.out.println(exporter.export());
        }
        System.out.println("患者ID: " + PatientIdKit.normalize(" p 042 "));
    }
}
