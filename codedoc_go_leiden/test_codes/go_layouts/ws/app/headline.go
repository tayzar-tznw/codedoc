package main

import (
	"strconv"

	"example.com/thirdparty/textfmt"
)

func headline() string {
	t := textfmt.NewTable()
	t.AddRow("ws", "app")
	rows := t.Len()
	return t.Render() + "rows=" + strconv.Itoa(rows) + "\n"
}
