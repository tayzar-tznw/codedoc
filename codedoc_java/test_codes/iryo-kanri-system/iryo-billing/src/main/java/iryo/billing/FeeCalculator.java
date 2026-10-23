package iryo.billing;

import java.math.BigDecimal;
import java.util.function.Supplier;

/**
 * 診療報酬の点数計算と窓口処理。
 */
public final class FeeCalculator {
    public int calculate(int a, int b) {
        return a + b;
    }

    public long calculate(long a, long b) {
        return a + b;
    }

    public double calculate(double a, double b) {
        return a + b;
    }

    public int calculate(int a, int b, int c) {
        return a + b + c;
    }

    public int calculate(int... points) {
        int total = 0;
        for (int point : points) {
            total += point;
        }
        return points.length > 3 ? (int) (total * 0.9) : total;
    }

    public String run(Runnable task) {
        task.run();
        return "同期実行";
    }

    public String run(Supplier<String> task) {
        task.get();
        return "非同期実行";
    }

    public int scale(int value, int factor) {
        return value * factor;
    }

    public double scale(int value, double factor) {
        return value * factor;
    }

    public BigDecimal pay(BigDecimal amount) {
        return amount;
    }

    public String bill(String receiptNumber) {
        return "番号請求: " + receiptNumber;
    }

    public String bill(Object item) {
        return "一般請求: " + item;
    }

    public String route(long patientNumber) {
        return "拡大変換受付: " + patientNumber;
    }

    public String route(Integer patientNumber) {
        return "ボックス化受付: " + patientNumber;
    }
}
