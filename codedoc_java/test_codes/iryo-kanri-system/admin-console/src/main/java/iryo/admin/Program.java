package iryo.admin;

/**
 * 管理コンソールの起動点。
 */
public final class Program {
    private Program() {
    }

    public static void main(String[] args) {
        System.out.println("=== 医療管理システム 管理コンソール ===");
        DailyBatch.run();
        ReportDigest.run();
        System.out.println("=== 管理タスク完了 ===");
    }
}
