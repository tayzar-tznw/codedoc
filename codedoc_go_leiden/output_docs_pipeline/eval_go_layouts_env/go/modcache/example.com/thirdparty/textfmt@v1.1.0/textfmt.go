// Package textfmt formats short texts and plain-text tables.
package textfmt

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

// Formatter turns one string into another.
type Formatter interface {
	Format(s string) string
}

// Upper is a Formatter that upper-cases letters.
type Upper struct{}

// Format returns s in upper case.
func (Upper) Format(s string) string {
	return strings.ToUpper(s)
}

// Bracket is a Formatter that wraps text in brackets.
type Bracket struct {
	Open, Close string
}

// Format returns s between b.Open and b.Close.
func (b Bracket) Format(s string) string {
	return b.Open + s + b.Close
}

// Apply runs every formatter over s in order.
func Apply(s string, fs ...Formatter) string {
	for _, f := range fs {
		s = f.Format(s)
	}
	return s
}

// PadRight pads s with spaces to width runes.
func PadRight(s string, width int) string {
	n := utf8.RuneCountInString(s)
	if n >= width {
		return s
	}
	return s + strings.Repeat(" ", width-n)
}

// Title upper-cases the first letter of each word.
func Title(s string) string {
	words := strings.Fields(s)
	for i, w := range words {
		r, size := utf8.DecodeRuneInString(w)
		words[i] = string(unicode.ToUpper(r)) + w[size:]
	}
	return strings.Join(words, " ")
}

// Table collects rows and renders them with aligned columns.
type Table struct {
	rows [][]string
}

// NewTable returns an empty table.
func NewTable() *Table {
	return &Table{}
}

// AddRow appends one row.
func (t *Table) AddRow(cells ...string) {
	t.rows = append(t.rows, cells)
}

// Render returns the table, one line per row, columns separated by " | ".
func (t *Table) Render() string {
	widths := map[int]int{}
	for _, row := range t.rows {
		for i, c := range row {
			if n := utf8.RuneCountInString(c); n > widths[i] {
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

// Len returns the number of rows, added in v1.1.0.
func (t *Table) Len() int {
	return len(t.rows)
}
