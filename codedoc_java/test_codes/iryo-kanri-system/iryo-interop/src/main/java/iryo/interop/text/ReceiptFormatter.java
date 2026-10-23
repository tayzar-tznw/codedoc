package iryo.interop.text;

/**
 * 外部連携向けの領収データ整形。
 */
public final class ReceiptFormatter {
    public String format(Object item) {
        return "[連携] " + item;
    }
}
