package iryo.kernel;

/**
 * 業務 API の引数を検証する共通ユーティリティ。
 */
public final class Guard {
    private Guard() {
    }

    public static <T> T notNull(T value, String parameterName) {
        if (value == null) {
            throw new IllegalArgumentException(parameterName + " は必須です。");
        }
        return value;
    }

    public static String notEmpty(String value, String parameterName) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(parameterName + " に空文字は指定できません。");
        }
        return value;
    }
}
