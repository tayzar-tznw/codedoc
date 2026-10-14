package testy

import (
	"os"
	"testing"

	"example.com/lib/trace"
)

type fakeClock struct{}

func (fakeClock) Now() int {
	trace.Hit()
	return 0
}

func TestIn(t *testing.T) {
	internalHelper()   //@TS01
	Stamp(fakeClock{}) //@TS02
	nowFn = func() int {
		trace.Hit()
		return 9
	}
	Tick() //@TS03
	nowFn = defaultNow
}

func TestMain(m *testing.M) {
	code := m.Run()
	trace.Dump()
	os.Exit(code)
}

func (Outer) Hook() string {
	trace.Hit()
	return "Outer(test)"
}

func TestHook(t *testing.T) {
	if got := CallHook(Outer{}); got != "Outer(test)" {
		t.Fatalf("got %q", got)
	}
}
