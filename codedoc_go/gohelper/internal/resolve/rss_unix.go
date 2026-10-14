//go:build linux || darwin || freebsd || netbsd || openbsd

package resolve

import (
	"runtime"
	"syscall"
)

// peakRSS is the helper's own peak resident set size in bytes.
func peakRSS() int64 {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return 0
	}
	if runtime.GOOS == "darwin" {
		return int64(ru.Maxrss)
	}
	return int64(ru.Maxrss) * 1024
}
