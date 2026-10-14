package scheduling

type SmsNotifier struct {
	Number string
	sent   int
}

func (s *SmsNotifier) Send(msg string) string {
	s.sent++
	return "SMS(" + s.Number + "): " + msg
}

func (s *SmsNotifier) Sent() int {
	return s.sent
}
