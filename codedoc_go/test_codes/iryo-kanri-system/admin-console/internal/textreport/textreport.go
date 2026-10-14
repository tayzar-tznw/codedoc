package textreport

import (
	"strconv"

	"example.com/thirdparty/textfmt"
)

func Heading(title string) string {
	return textfmt.Apply(title, textfmt.Upper{}, textfmt.Bracket{Open: "【", Close: "】"})
}

func Render(title string, rows [][2]string) string {
	t := textfmt.NewTable()
	for _, r := range rows {
		t.AddRow(r[0], r[1])
	}
	return textfmt.Title(title) + "\n" + t.Render()
}

func Line(label string, yen int) string {
	return textfmt.PadRight(label, 12) + strconv.Itoa(yen) + "円"
}
