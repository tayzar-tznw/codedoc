//go:build !cgo

package platform

func Accel() string {
	return "pure"
}
