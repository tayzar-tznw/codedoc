package escalation

import "testing"

func (Policy) Level() string {
	return "試験"
}

func TestEvaluate(t *testing.T) {
	if got := Evaluate(Policy{Ward: "東"}); got != "東:試験" {
		t.Fatalf("Evaluate = %q", got)
	}
}
