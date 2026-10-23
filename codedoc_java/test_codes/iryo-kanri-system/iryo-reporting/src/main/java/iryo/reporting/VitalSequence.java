package iryo.reporting;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Function;
import java.util.function.Predicate;

/**
 * バイタル値を業務ルールに沿って連続変換する専用列。
 */
public final class VitalSequence {
    private final List<Integer> values;

    public VitalSequence(int... values) {
        this.values = new ArrayList<>();
        for (int value : values) {
            this.values.add(value);
        }
    }

    private VitalSequence(List<Integer> values) {
        this.values = values;
    }

    public VitalSequence filter(Predicate<Integer> predicate) {
        List<Integer> selected = new ArrayList<>();
        for (Integer value : values) {
            if (predicate.test(value)) {
                selected.add(value);
            }
        }
        return new VitalSequence(selected);
    }

    public VitalSequence map(Function<Integer, Integer> mapper) {
        List<Integer> mapped = new ArrayList<>();
        for (Integer value : values) {
            mapped.add(mapper.apply(value));
        }
        return new VitalSequence(mapped);
    }

    public int total() {
        int total = 0;
        for (Integer value : values) {
            total += value;
        }
        return total;
    }
}
