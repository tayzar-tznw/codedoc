package tariff

import (
	"strconv"

	"example.com/thirdparty/yamlish"
)

const source = "A000: 288\nA001: 73\nB001: 225\n"

type Table struct {
	doc *yamlish.Document
}

func Load() (*Table, error) {
	doc, err := yamlish.Parse(source)
	if err != nil {
		return nil, err
	}
	return &Table{doc: doc}, nil
}

func (t *Table) Points(code string) int {
	v, ok := t.doc.Get(code)
	if !ok {
		return 0
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return 0
	}
	return n
}

func (t *Table) Codes() []string {
	return t.doc.Keys()
}
