package main

import (
	"fmt"
	"os"
	"runtime"
	"strings"
	"sync"
)

var mu sync.Mutex

func short(s string) string {
	if i := strings.LastIndex(s, "/"); i >= 0 {
		return s[i+1:]
	}
	return s
}

//go:noinline
func hk(tag string) {
	pcs := make([]uintptr, 6)
	n := runtime.Callers(2, pcs)
	fr := runtime.CallersFrames(pcs[:n])
	var parts []string
	for i := 0; i < 4; i++ {
		f, more := fr.Next()
		parts = append(parts, fmt.Sprintf("%s@%s:%d", f.Function, short(f.File), f.Line))
		if !more {
			break
		}
	}
	created := ""
	if len(parts) >= 2 && strings.HasPrefix(parts[1], "runtime.goexit") {
		buf := make([]byte, 4096)
		m := runtime.Stack(buf, false)
		s := string(buf[:m])
		if i := strings.Index(s, "created by "); i >= 0 {
			created = " CREATED=" + strings.ReplaceAll(strings.TrimSpace(s[i:]), "\n\t", " @ ")
		}
	}
	mu.Lock()
	fmt.Fprintf(os.Stdout, "%-14s callee=%s | caller=%s%s\n", tag, parts[0], strings.Join(parts[1:], " <- "), created)
	mu.Unlock()
}
