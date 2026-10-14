package extlib

import "strconv"

type Label struct {
	text string
}

func NewLabel(n int) Label {
	return Label{text: "outside-" + strconv.Itoa(n)}
}

func (l Label) Text() string {
	return l.text
}
