package defs

import "example.com/thirdparty/zap"

type Logger interface{ Log(c zap.Core) string }

type MyLog struct{}

func (MyLog) Log(c zap.Core) string { return "my" }

type Other struct{}

func (Other) Log(c fmtCore) string { return "other" }

type fmtCore interface{ Format() string }
