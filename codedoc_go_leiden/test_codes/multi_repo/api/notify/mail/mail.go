package mail

import "example.com/iryo/shared/log"

var outbox = log.New("mail")

func Compose(body string) string {
	outbox.Info(body)
	return "mail:" + body
}
