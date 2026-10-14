package tally

import (
	"strconv"

	"example.com/thirdparty/textfmt"
)

func Label(c *Counter) string {
	return textfmt.Title("tally total") + " " + strconv.Itoa(c.Total())
}
