package notify

type Notifier interface {
	Send(msg string) string
}

type TrackedNotifier interface {
	Notifier
	Sent() int
}

type MailNotifier struct {
	To string
}

func (m MailNotifier) Send(msg string) string {
	return "メール(" + m.To + "): " + msg
}

func Broadcast(ns []Notifier, msg string) []string {
	var out []string
	for _, n := range ns {
		out = append(out, n.Send(msg))
	}
	return out
}

func Relay(tn TrackedNotifier, msg string) string {
	line := tn.Send(msg)
	return line + " (送信済み " + itoa(tn.Sent()) + " 件)"
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var digits []byte
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	return string(digits)
}
