package iryo.legacy.claims.text;

/**
 * 旧レセコン互換の領収書整形。
 */
public final class ReceiptFormatter {
    public String format(Object item) {
        return "[旧レセ] " + item;
    }
}
