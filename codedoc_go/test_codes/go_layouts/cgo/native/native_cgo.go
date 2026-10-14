package native

import "C"

import "strings"

func Version() string {
	return strings.ToUpper(C.GoString(C.CString("cgo")))
}
