package crossb

import "example.com/thirdparty/zap"

type Inner struct{}

func (Inner) Info(msg string) {}

type Mid struct{ Inner }

type S struct {
	zap.Sugar
	Mid
}
