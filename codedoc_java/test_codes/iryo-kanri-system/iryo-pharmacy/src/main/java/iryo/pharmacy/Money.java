package iryo.pharmacy;

import java.math.BigDecimal;

/**
 * 薬局在庫の評価額。
 */
public final class Money {
    public final BigDecimal amount;

    public Money(BigDecimal amount) {
        this.amount = amount;
    }
}
