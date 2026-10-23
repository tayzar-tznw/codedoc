package iryo.scheduling;

import iryo.kernel.Notifier;

/**
 * 予約部門が提供する SMS 通知。
 */
public final class SmsNotifier implements Notifier {
    @Override
    public void send(String msg) {
        System.out.println("SMS送信: " + msg);
    }
}
