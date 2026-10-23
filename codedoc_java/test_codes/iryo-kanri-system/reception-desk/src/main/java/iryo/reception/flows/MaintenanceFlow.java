package iryo.reception.flows;

import iryo.billing.FeeCalculator;
import iryo.data.ChartCache;
import iryo.data.Registry;
import iryo.domain.AuditedEntity;
import iryo.domain.ChartArchive;
import iryo.domain.ChartEntry;
import iryo.domain.Entity;

import static iryo.billing.PointMath.toYen;

/**
 * キャッシュ、アーカイブ、点数表の保守点検を行う。
 */
public final class MaintenanceFlow {
    private MaintenanceFlow() {
    }

    public static void run() {
        System.out.println("-- 保守 --");

        var karte = new ChartCache<String, ChartEntry>();
        karte.put("外来", new ChartEntry(7, "経過良好"));
        ChartEntry entry = karte.get("外来");
        System.out.println("カルテキャッシュ: 患者" + entry.patientId());
        System.out.println("カルテ内容: " + karte.get("外来").text());

        ChartEntry created = Registry.<ChartEntry>create(ChartEntry.class);
        System.out.println("新規カルテ: " + created.text());

        var oldCache = new iryo.legacy.claims.ChartCache<Integer>();
        oldCache.put(32);
        System.out.println("旧集計キャッシュ: " + oldCache.get());

        new ChartArchive.Compactor().start();
        var archive = new ChartArchive();
        var janitor = archive.new Janitor();
        janitor.clean();

        Entity plain = Entity.create();
        AuditedEntity audited = AuditedEntity.create();
        System.out.println("生成種別: " + plain.getClass().getSimpleName()
                + " / " + audited.getClass().getSimpleName());
        System.out.println("監査表示: " + inspect(audited));

        int overloads = 0;
        for (var m : FeeCalculator.class.getMethods()) {
            if (m.getName().equals("calculate")) {
                overloads++;
            }
        }
        System.out.println("calculate 系統: " + overloads + " 種");

        int probe = toYen(10);
        System.out.println("換算点検: " + probe + "円");
    }

    private static <T extends Entity> String inspect(T entity) {
        return entity.describe();
    }

    private static int toYen(int points) {
        return points * 11;
    }
}
