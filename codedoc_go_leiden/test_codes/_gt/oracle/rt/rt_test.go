package rt

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

//go:noinline
func hooked(fid uint32) { E(fid) }

//go:noinline
func hookedThenClose(fid uint32, done chan struct{}) {
	E(fid)
	close(done)
}

func line() int {
	_, _, l, _ := runtime.Caller(1)
	return l
}

type hitRec struct {
	Kind, Callee, Caller, File string
	Fid, Line                  int
	N                          uint64
}

func TestHooksFastSlowAndGoroutine(t *testing.T) {
	if runtime.GOARCH != "amd64" {
		t.Skip("the fast path is amd64 only")
	}
	if !calibrated.Load() || !fastOK {
		t.Fatalf("calibration failed: bpOff %d", bpOff)
	}
	dir := t.TempDir()
	t.Setenv("GORACLE_OUT", dir)
	enabled.Store(true)
	lineA := line() + 1
	hooked(7)
	hooked(7)
	lineB := line() + 1
	hooked(8)
	fastOK = false
	lineC := line() + 1
	hooked(9)
	fastOK = true
	done := make(chan struct{})
	lineGo := line() + 1
	go hookedThenClose(10, done)
	<-done
	D(3)
	D(3)
	enabled.Store(false)
	write(false)

	f, err := os.Open(filepath.Join(dir, "hits-"+strconv.Itoa(os.Getpid())+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	got := map[string]uint64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var r hitRec
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			t.Fatal(err)
		}
		switch r.Kind {
		case "call", "go":
			if !strings.HasSuffix(r.Callee, "rt.hooked") && !strings.HasSuffix(r.Callee, "rt.hookedThenClose") {
				t.Errorf("callee name %q", r.Callee)
			}
			got[r.Kind+" "+strconv.Itoa(r.Fid)+" "+filepath.Base(r.File)+":"+strconv.Itoa(r.Line)] += r.N
		case "done":
			got["done "+strconv.Itoa(r.Fid)] += r.N
		}
	}
	want := map[string]uint64{
		"call 7 rt_test.go:" + strconv.Itoa(lineA):   1,
		"call 7 rt_test.go:" + strconv.Itoa(lineA+1): 1,
		"call 8 rt_test.go:" + strconv.Itoa(lineB):   1,
		"call 9 rt_test.go:" + strconv.Itoa(lineC):   1,
		"go 10 rt_test.go:" + strconv.Itoa(lineGo):   1,
		"done 3": 2,
	}
	for k, n := range want {
		if got[k] != n {
			t.Errorf("%s: %d, want %d (all: %v)", k, got[k], n, got)
		}
	}
	if len(got) != len(want) {
		t.Errorf("records %v, want %v", got, want)
	}
}

func TestParseCreatedBy(t *testing.T) {
	stack := "goroutine 7 [running]:\nmain.f(...)\n\t/a/b.go:10 +0x25\ncreated by main.main in goroutine 1\n\t/a/main.go:20 +0x4e\n"
	if fn, file, l := parseCreatedBy([]byte(stack)); fn != "main.main" || file != "/a/main.go" || l != 20 {
		t.Errorf("got %s %s %d", fn, file, l)
	}
	old := "goroutine 7 [running]:\ncreated by main.g\n\t/a/g.go:3\n"
	if fn, file, l := parseCreatedBy([]byte(old)); fn != "main.g" || file != "/a/g.go" || l != 3 {
		t.Errorf("got %s %s %d", fn, file, l)
	}
	if fn, _, l := parseCreatedBy([]byte("goroutine 1 [running]:\nmain.main()\n")); fn != "?" || l != 0 {
		t.Errorf("got %s %d", fn, l)
	}
}
