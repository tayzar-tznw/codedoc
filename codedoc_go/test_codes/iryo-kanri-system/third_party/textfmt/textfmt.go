package textfmt

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

type Formatter interface {
	Format(s string) string
}

type Upper struct{}

func (Upper) Format(s string) string {
	return strings.ToUpper(s)
}

type Bracket struct {
	Open, Close string
}

func (b Bracket) Format(s string) string {
	return b.Open + s + b.Close
}

func Apply(s string, fs ...Formatter) string {
	for _, f := range fs {
		s = f.Format(s)
	}
	return s
}

func displayWidth(s string) int {
	w := 0
	for _, r := range s {
		if unicode.In(r, unicode.Han, unicode.Hiragana, unicode.Katakana) || r >= 0xFF01 && r <= 0xFF60 {
			w += 2
		} else {
			w++
		}
	}
	return w
}

func PadRight(s string, width int) string {
	n := displayWidth(s)
	if n >= width {
		return s
	}
	return s + strings.Repeat(" ", width-n)
}

func Title(s string) string {
	words := strings.Fields(s)
	for i, w := range words {
		r, size := utf8.DecodeRuneInString(w)
		words[i] = string(unicode.ToUpper(r)) + w[size:]
	}
	return strings.Join(words, " ")
}

type Table struct {
	rows [][]string
}

func NewTable() *Table {
	return &Table{}
}

func (t *Table) AddRow(cells ...string) {
	t.rows = append(t.rows, cells)
}

func (t *Table) Render() string {
	widths := map[int]int{}
	for _, row := range t.rows {
		for i, c := range row {
			if n := displayWidth(c); n > widths[i] {
				widths[i] = n
			}
		}
	}
	var b strings.Builder
	for _, row := range t.rows {
		for i, c := range row {
			if i > 0 {
				b.WriteString(" | ")
			}
			b.WriteString(PadRight(c, widths[i]))
		}
		b.WriteString("\n")
	}
	return b.String()
}

func (t *Table) Len() int {
	return len(t.rows)
}
