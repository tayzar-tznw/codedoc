//go:build iryodebug

package platform

import "fmt"

func Debug() string {
	return fmt.Sprintf("debug:%s", Name())
}
