package low

import (
	"time"
	_ "unsafe"

	"example.com/cls/hidden"
)

//go:linkname runtimeNanotime runtime.nanotime
func runtimeNanotime() int64

//go:linkname hiddenSecret example.com/cls/hidden.secret
func hiddenSecret() string

func Add(a, b int) int

//go:linkname valueMethod example.com/cls/hidden.T.Value
func valueMethod(t hidden.T) int

//go:linkname ptrMethod example.com/cls/hidden.(*T).Ptr
func ptrMethod(t *hidden.T) int

func Calls() {
	_ = runtimeNanotime()
	_ = hiddenSecret()
	_ = Add(1, 2)
	_ = time.Now()
	_ = valueMethod(hidden.T{})
	_ = ptrMethod(&hidden.T{})
}
