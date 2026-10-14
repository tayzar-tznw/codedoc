package lib

import "testing"

func TestDispatch(t *testing.T) {
	if got := Dispatch(); got == 0 {
		t.Fatal("zero")
	}
	if Add(One(), 1) != 2 {
		t.Fatal("add")
	}
}
