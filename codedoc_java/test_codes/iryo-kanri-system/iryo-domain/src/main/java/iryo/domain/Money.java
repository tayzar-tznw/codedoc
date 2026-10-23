package iryo.domain;

import java.math.BigDecimal;
import java.util.Objects;

/**
 * 円建て金額を表す不変値。
 */
public record Money(BigDecimal amount) {
    public Money {
        Objects.requireNonNull(amount, "amount");
    }

    public Money plus(Money other) {
        return new Money(amount.add(other.amount));
    }

    @Override
    public String toString() {
        return amount.toPlainString() + "円";
    }
}
