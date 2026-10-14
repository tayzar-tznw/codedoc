// Package yamlish reads and writes flat "key: value" documents.
package yamlish

import (
	"errors"
	"sort"
	"strings"
)

// ErrSyntax reports a line without a colon.
var ErrSyntax = errors.New("yamlish: line without colon")

// Encoder is anything that can render itself as a document.
type Encoder interface {
	Encode() string
}

// Document is an ordered set of keys and values.
type Document struct {
	keys   []string
	values map[string]string
}

// NewDocument returns an empty document.
func NewDocument() *Document {
	return &Document{values: map[string]string{}}
}

// Set stores v under k; a key keeps the position of its first Set.
func (d *Document) Set(k, v string) {
	if _, ok := d.values[k]; !ok {
		d.keys = append(d.keys, k)
	}
	d.values[k] = v
}

// Get returns the value under k.
func (d *Document) Get(k string) (string, bool) {
	v, ok := d.values[k]
	return v, ok
}

// Encode renders the document, one "key: value" line per key.
func (d *Document) Encode() string {
	var b strings.Builder
	for _, k := range d.keys {
		b.WriteString(k + ": " + d.values[k] + "\n")
	}
	return b.String()
}

// Marshal renders m with sorted keys.
func Marshal(m map[string]string) string {
	d := NewDocument()
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		d.Set(k, m[k])
	}
	return d.Encode()
}

// Unmarshal parses a document into a map.
func Unmarshal(text string) (map[string]string, error) {
	d, err := Parse(text)
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, k := range d.keys {
		out[k] = d.values[k]
	}
	return out, nil
}

// Parse reads a document; blank lines and lines starting with # are skipped.
func Parse(text string) (*Document, error) {
	d := NewDocument()
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			return nil, ErrSyntax
		}
		d.Set(strings.TrimSpace(k), strings.TrimSpace(v))
	}
	return d, nil
}

// Write returns the document form of e.
func Write(e Encoder) string {
	return e.Encode()
}
