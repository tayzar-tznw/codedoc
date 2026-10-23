package iryo.data;

import java.lang.reflect.InvocationTargetException;

/**
 * public な引数なしコンストラクタから台帳部品を作成する。
 */
public final class Registry {
    private Registry() {
    }

    public static <T> T create(Class<T> type) {
        try {
            return type.getConstructor().newInstance();
        } catch (InstantiationException
                | IllegalAccessException
                | InvocationTargetException
                | NoSuchMethodException error) {
            throw new IllegalArgumentException("型を生成できません: " + type.getName(), error);
        }
    }
}
