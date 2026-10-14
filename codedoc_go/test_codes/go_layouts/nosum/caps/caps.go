package caps

import "example.com/thirdparty/textfmt"

func Caps(s string) string {
	return textfmt.Title(s)
}
