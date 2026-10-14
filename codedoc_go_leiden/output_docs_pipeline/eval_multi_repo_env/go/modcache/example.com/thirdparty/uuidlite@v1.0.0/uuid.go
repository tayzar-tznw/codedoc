// Package uuidlite generates and parses small deterministic UUIDs.
package uuidlite

import (
	"encoding/hex"
	"errors"
	"strings"
)

// UUID is a 16-byte identifier.
type UUID [16]byte

// ErrFormat reports a string that is not a UUID in canonical form.
var ErrFormat = errors.New("uuidlite: invalid format")

// Generator produces UUIDs.
type Generator interface {
	Next() UUID
}

// Sequence is a Generator that counts up from a seed.
type Sequence struct {
	seed  uint64
	count uint64
}

// NewSequence returns a Sequence that starts after seed.
func NewSequence(seed uint64) *Sequence {
	return &Sequence{seed: seed}
}

// Next returns the next UUID of the sequence.
func (s *Sequence) Next() UUID {
	s.count++
	return FromInt(s.seed + s.count)
}

var defaultSequence = NewSequence(0)

// New returns the next UUID of the package's default sequence.
func New() UUID {
	return defaultSequence.Next()
}

// NewFrom returns the next UUID of g.
func NewFrom(g Generator) UUID {
	return g.Next()
}

// FromInt builds a version 4 layout UUID whose low bytes hold n.
func FromInt(n uint64) UUID {
	var u UUID
	for i := 0; i < 8; i++ {
		u[15-i] = byte(n >> (8 * i))
	}
	u[6] = (u[6] & 0x0f) | 0x40
	u[8] = (u[8] & 0x3f) | 0x80
	return u
}

// Parse reads the canonical 36-character form.
func Parse(s string) (UUID, error) {
	var u UUID
	if len(s) != 36 || s[8] != '-' || s[13] != '-' || s[18] != '-' || s[23] != '-' {
		return u, ErrFormat
	}
	raw, err := hex.DecodeString(strings.ReplaceAll(s, "-", ""))
	if err != nil {
		return u, ErrFormat
	}
	copy(u[:], raw)
	return u, nil
}

// MustParse is Parse that panics on error.
func MustParse(s string) UUID {
	u, err := Parse(s)
	if err != nil {
		panic(err)
	}
	return u
}

// String returns the canonical form.
func (u UUID) String() string {
	h := hex.EncodeToString(u[:])
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:32]
}

// IsZero reports whether u is the zero UUID.
func (u UUID) IsZero() bool {
	return u == UUID{}
}
