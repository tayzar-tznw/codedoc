package iryo.data;

import java.util.HashMap;
import java.util.Map;

/**
 * 診療科などのキーでカルテ情報を保持するキャッシュ。
 */
public final class ChartCache<K, V> {
    private final Map<K, V> values = new HashMap<>();

    public V get(K key) {
        return values.get(key);
    }

    public void put(K key, V value) {
        values.put(key, value);
    }
}
