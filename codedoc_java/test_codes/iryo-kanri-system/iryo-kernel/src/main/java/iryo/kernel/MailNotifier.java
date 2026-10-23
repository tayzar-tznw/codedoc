package iryo.kernel;

/**
 * 院内既定のメール通知。
 */
public final class MailNotifier implements Notifier {
    @Override
    public void send(String msg) {
        System.out.println("メール送信: " + msg);
    }
}
