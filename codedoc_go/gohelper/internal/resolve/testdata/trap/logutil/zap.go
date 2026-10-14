package logutil

import "example.com/thirdparty/zap"

func CreateLogger() *zap.Logger { return zap.New() }

func Use() string { return Helper() }

type Thing struct{}

func (Thing) Other() string { return "other" }
