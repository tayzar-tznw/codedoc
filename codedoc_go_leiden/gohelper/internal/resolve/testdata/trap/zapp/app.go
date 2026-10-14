package zapp

import (
	"example.com/thirdparty/zap"
	"example.com/trap/logutil"
)

func Run() string {
	_ = logutil.CreateLogger()
	_ = zap.New()
	return logutil.Helper()
}
