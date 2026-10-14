//go:build !(linux || darwin || freebsd || netbsd || openbsd)

package resolve

func peakRSS() int64 { return 0 }
