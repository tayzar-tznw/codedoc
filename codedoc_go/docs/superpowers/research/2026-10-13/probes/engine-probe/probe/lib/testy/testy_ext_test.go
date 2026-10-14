package testy_test

import (
	"testing"

	"example.com/lib/testy"
)

func TestExt(t *testing.T) {
	testy.InternalHelper()       //@TS04
	testy.ExportedForTest()      //@TS08
	testy.Stamp(testy.Default()) //@TS09
	testy.Tick()                 //@TS10
}
