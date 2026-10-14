//go:build !cgo

package native

func Version() string {
	return "pure"
}
