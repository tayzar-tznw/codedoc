package iryo.reception;

import iryo.data.Repository;
import iryo.data.SqlPatientRepository;
import iryo.kernel.MailNotifier;
import iryo.kernel.Notifier;
import iryo.kernel.ServiceCollection;
import iryo.kernel.ServiceProvider;
import iryo.kernel.logging.Logger;
import iryo.reception.flows.AccountingFlow;
import iryo.reception.flows.CheckInFlow;
import iryo.reception.flows.DailyCloseFlow;
import iryo.reception.flows.MaintenanceFlow;
import iryo.reception.flows.PharmacyFlow;
import iryo.reception.flows.ReportFlow;
import iryo.scheduling.SmsNotifier;

/**
 * 受付端末の起動点。
 */
public final class Program {
    private Program() {
    }

    public static void main(String[] args) {
        ServiceProvider services = buildContainer();

        System.out.println("=== 医療管理システム 受付端末 ===");
        CheckInFlow.run(services);
        AccountingFlow.run();
        PharmacyFlow.run();
        ReportFlow.run();
        MaintenanceFlow.run();
        DailyCloseFlow.run();
        System.out.println("=== 受付業務デモ完了 ===");
    }

    /**
     * 受付端末で使用するサービスを登録する合成ルート。
     */
    public static ServiceProvider buildContainer() {
        var services = new ServiceCollection();
        services.addSingleton(Notifier.class, MailNotifier.class);
        services.addSingleton(Notifier.class, SmsNotifier.class);
        services.addSingleton(Repository.class, SqlPatientRepository.class);
        services.addSingleton(Logger.class, () -> new Logger("受付端末"));
        return services.buildProvider();
    }
}
