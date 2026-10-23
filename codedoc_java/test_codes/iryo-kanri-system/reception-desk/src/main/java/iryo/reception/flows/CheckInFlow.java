package iryo.reception.flows;

import iryo.data.Repository;
import iryo.data.RepositoryFactory;
import iryo.domain.AuditedEntity;
import iryo.domain.Entity;
import iryo.domain.Patient;
import iryo.kernel.Notifier;
import iryo.kernel.ServiceProvider;

/**
 * 来院受付と患者台帳照会を行う。
 */
public final class CheckInFlow {
    private CheckInFlow() {
    }

    public static void run(ServiceProvider services) {
        System.out.println("-- 受付 --");

        var notifier = services.get(Notifier.class);
        notifier.send("受付を開始しました");
        System.out.println("利用可能な通知手段: " + services.getAll(Notifier.class).size());

        Repository<?> repo = services.get(Repository.class);
        var waiting = (Patient) repo.find(1);
        System.out.println(waiting.describe());

        var defaultRepo = RepositoryFactory.createDefault();
        var patient = defaultRepo.find(2);
        var summary = patient.describe();
        System.out.println(summary);
        System.out.println("接続確認: " + defaultRepo.ping());

        AuditedEntity ledgerView = patient;
        System.out.println("台帳表示: " + ledgerView.describe());
        patient.touch();
        patient.audit("受付済");

        Patient copy = patient.clone();
        Entity general = ((Entity) patient).clone();
        System.out.println("複製: " + copy.describe() + " / 基底複製 " + general.describe());
    }
}
