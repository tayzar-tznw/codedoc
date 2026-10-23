package iryo.billing.text;

/**
 * 現行会計向けの領収書整形。
 */
public final class ReceiptFormatter {
    public String format(Object item) {
        return "[会計] " + item;
    }
}
