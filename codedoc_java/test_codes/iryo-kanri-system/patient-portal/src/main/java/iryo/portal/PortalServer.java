package iryo.portal;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import iryo.billing.FeeCalculator;
import iryo.billing.PointMath;
import iryo.domain.Appointment;
import iryo.kernel.MailNotifier;
import iryo.kernel.Notifier;
import iryo.scheduling.AppointmentBook;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;

/**
 * 患者向け予約・料金照会サーバー。
 */
public final class PortalServer {
    private final HttpServer server;
    private final AppointmentBook appointmentBook = new AppointmentBook();
    private final FeeCalculator feeCalculator = new FeeCalculator();
    private final Notifier notifier = new MailNotifier();

    public PortalServer(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/appointments", this::handleAppointments);
        server.createContext("/fees", this::handleFees);
    }

    public void start() {
        server.start();
        System.out.println("患者ポータルを開始しました: " + server.getAddress());
    }

    private void handleAppointments(HttpExchange exchange) throws IOException {
        var appointment = appointmentBook.reserve(
                new Appointment(1, 42, LocalDateTime.now().plusDays(7)));
        notifier.send("予約確認: " + appointment.summary());
        respond(exchange, 200, appointment.summary());
    }

    private void handleFees(HttpExchange exchange) throws IOException {
        int points = feeCalculator.calculate(288, 146);
        int yen = PointMath.toYen(points);
        notifier.send("料金目安を照会しました");
        respond(exchange, 200, "料金目安: " + points + "点 / " + yen + "円");
    }

    private static void respond(HttpExchange exchange, int status, String body) throws IOException {
        byte[] payload = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set(
                "Content-Type",
                "text/plain; charset=" + StandardCharsets.UTF_8.name());
        exchange.sendResponseHeaders(status, payload.length);
        try (var response = exchange.getResponseBody()) {
            response.write(payload);
        }
    }

    public static void main(String[] args) throws IOException {
        new PortalServer(8080).start();
    }
}
