package iryo.reporting;

import java.util.Arrays;

/**
 * カルテ帳票の見出しと集計値を整える静的ユーティリティ。
 */
public final class KarteKit {
    private KarteKit() {
    }

    public static String heading(String title) {
        return "【" + title + "】";
    }

    public static int average(int... values) {
        return values.length == 0
                ? 0
                : (int) Math.round(Arrays.stream(values).average().orElse(0));
    }
}
