package textkit

import "testing"

func TestFrame(t *testing.T) {
	if got := Frame("a"); got != "[== A ==]" {
		t.Fatalf("Frame() = %q", got)
	}
}
