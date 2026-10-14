package off

import (
	"github.com/go-logr/logr"
	"github.com/google/uuid"
)

var DeepHits []string

type Deep struct{}

func (Deep) Info(msg string, kv ...any) { DeepHits = append(DeepHits, "Deep.Info:"+msg) }

func (Deep) Enabled() bool {
	DeepHits = append(DeepHits, "Deep.Enabled")
	return true
}

type Base struct{ Deep }

type Svc struct {
	logr.Logger
	Base
}

func UseSvc(s Svc) {
	s.Info("x")  //@OF01
	s.Enabled()  //@OF02
}

type Svc2 struct {
	*logr.Logger
	Base
}

func UseSvc2(s Svc2) { s.Info("y") } //@OF03

type Namer interface{ Name(u uuid.UUID) string }

type ByInt struct{}

func (ByInt) Name(u int) string { return "int" }

type ByString struct{}

func (ByString) Name(s string) string { return "string" }

type ByUUID struct{}

func (ByUUID) Name(u uuid.UUID) string { return u.String() }

func UseNamer(n Namer) string { return n.Name(uuid.UUID{}) } //@OF04

type Sink interface {
	logr.LogSink
	Flush()
}

type OnlyFlush struct{}

func (OnlyFlush) Flush() {}

type FullSink struct{}

func (FullSink) Init(info logr.RuntimeInfo)                 {}
func (FullSink) Enabled(level int) bool                     { return true }
func (FullSink) Info(level int, msg string, kv ...any)      {}
func (FullSink) Error(err error, msg string, kv ...any)     {}
func (FullSink) WithValues(kv ...any) logr.LogSink          { return FullSink{} }
func (FullSink) WithName(name string) logr.LogSink          { return FullSink{} }
func (FullSink) Flush()                                     {}

func UseSink(s Sink) { s.Flush() } //@OF05

func ID() string { return uuid.NewString() } //@OF06

func UseLogger(l logr.Logger) { l.Info("z") } //@OF07

func Plain() string { return ID() } //@OF08

type ByOther struct{}

func (ByOther) Name(l logr.Logger) string { return "other" }
