package iryo.kernel;

/**
 * 患者や職員へ連絡する手段の抽象。
 */
public interface Notifier {
    void send(String msg);
}
