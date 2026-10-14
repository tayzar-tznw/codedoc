package lib_test

import (
	"testing"

	"example.com/probe3/lib"
)

func helper() int { return lib.Two() }

func TestExternal(t *testing.T) {
	if lib.Add(helper(), lib.One()) != 3 {
		t.Fatal("ext")
	}
}
