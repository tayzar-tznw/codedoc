package iryo.legacy.claims;

/**
 * 旧レセコンが単一値だけを保持するために使う簡易キャッシュ。
 */
public final class ChartCache<T> {
    private T value;

    public T get() {
        return value;
    }

    public void put(T value) {
        this.value = value;
    }
}
