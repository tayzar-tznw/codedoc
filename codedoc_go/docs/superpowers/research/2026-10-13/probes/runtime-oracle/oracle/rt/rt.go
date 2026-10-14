// Package rt is the runtime half of goracle. The instrumenter makes every
// function body and func literal in the copied tree start with E(fid). E records
// the pair (fid, return PC of the caller's call instruction) and counts it. At
// process exit the pairs are resolved to frames and written to
// $GORACLE_OUT/hits-<pid>-<n>.jsonl. Standard library only.
//
// Build the instrumented tree with -gcflags=all=-l -ldflags=-checklinkname=0:
// the exit hook is reached through a linkname to internal/runtime/exithook.
package rt

import (
	"bufio"
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unsafe"
)

const (
	tableBits = 20
	tableSize = 1 << tableBits
	pcBits    = 40
)

var (
	keys   [tableSize]atomic.Uint64 // 0 = empty; else (fid+1)<<pcBits | pc
	counts [tableSize]atomic.Uint64

	slowMu sync.Mutex
	slow   = map[uint64]uint64{} // table overflow or pc >= 1<<pcBits (key packs fid and pc lossily; see slowPCs)
	slowPC = map[uint64][2]uint64{}

	fidMu  sync.Mutex
	fidPC  = map[uint32]uintptr{} // first PC seen inside each instrumented function (for its runtime name)
	goMu   sync.Mutex
	goHits = map[goKey]uint64{}

	goexitPC uintptr // return PC into runtime.goexit, the same for every goroutine
	enabled  atomic.Bool
	flushMu  sync.Mutex
	flushed  atomic.Bool
	seq      atomic.Uint64
)

type goKey struct {
	fid       uint32
	createdBy string // function that ran the go statement
	file      string
	line      int
}

// E is called first in every instrumented function body.
//
// Fast path: the caller's return PC is read through the frame-pointer chain
// (E's saved BP is the instrumented function's BP; one word above that
// function's own saved BP is its return address). The first time a pair
// (fid, return PC) is seen it is checked against runtime.Callers, which elides
// wrapper frames; a pair that disagrees is marked indirect and always takes the
// runtime.Callers path. Needs -gcflags=all=-l (an inlined callee has no frame).
//
//go:noinline
func E(fid uint32) {
	if !enabled.Load() {
		return
	}
	var anchor uintptr
	if !fastOK {
		if fid == calibFid {
			calibrateIn(uintptr(unsafe.Pointer(&anchor)))
			return
		}
		slowEnter(fid)
		return
	}
	base := uintptr(unsafe.Pointer(&anchor)) + bpOff
	fbp := *(*uintptr)(unsafe.Pointer(base))
	pc := *(*uintptr)(unsafe.Pointer(fbp + 8))
	if pc == goexitPC || uint64(pc) >= 1<<pcBits {
		slowEnter(fid)
		return
	}
	key := uint64(fid+1)<<pcBits | uint64(pc)
	h := hash(key) & (tableSize - 1)
	for i := 0; i < 128; i++ {
		k := keys[h].Load()
		if k == key {
			if counts[h].Add(1)&indirect != 0 {
				slowEnter(fid)
			}
			return
		}
		if k == 0 {
			if keys[h].CompareAndSwap(0, key) {
				firstSeen(fid, pc, h)
				return
			}
			i--
			continue // somebody filled this slot; look at it again
		}
		h = (h + 1) & (tableSize - 1)
	}
	slowEnter(fid)
}

const indirect = 1 << 63

const doneSize = 1 << 22

var doneCounts [doneSize]atomic.Uint64

// D counts one completion of the simple statement id.
//
//go:noinline
func D(id uint32) {
	if id < doneSize && enabled.Load() {
		doneCounts[id].Add(1)
	}
}

var (
	fastOK     bool
	bpOff      uintptr
	calibrated atomic.Bool
	calibPC    [2]uintptr
	fastChecks atomic.Uint64 // pairs checked against runtime.Callers
	fastDiffer atomic.Uint64 // pairs where the frame-pointer PC was not the logical caller
)

// firstSeen checks a new fast-path pair against runtime.Callers.
//
//go:noinline
func firstSeen(fid uint32, pc uintptr, slot uint64) {
	var pcs [2]uintptr
	// skip 3: runtime.Callers, firstSeen, E
	n := runtime.Callers(3, pcs[:])
	fastChecks.Add(1)
	if n == 2 && pcs[1] == pc {
		counts[slot].Add(1)
		noteFid(fid, pcs[0])
		return
	}
	fastDiffer.Add(1)
	counts[slot].Add(indirect)
	if n == 2 {
		record(fid, pcs[0], pcs[1])
	}
}

//go:noinline
func slowEnter(fid uint32) {
	var pcs [2]uintptr
	// skip 3: runtime.Callers, slowEnter, E -> pcs[0] is the instrumented
	// function, pcs[1] the logical caller. Wrapper frames (method wrappers,
	// -fm method values, go/defer wrappers) are elided by runtime.Callers.
	if runtime.Callers(3, pcs[:]) < 2 {
		return
	}
	record(fid, pcs[0], pcs[1])
}

// record counts (fid, pc) under the slow path; pc is a logical caller.
func record(fid uint32, self, pc uintptr) {
	if pc == goexitPC {
		goEntry(fid, self)
		return
	}
	if uint64(pc) >= 1<<pcBits {
		slowAdd(fid, self, pc)
		return
	}
	key := uint64(fid+1)<<pcBits | uint64(pc)
	h := hash(key) & (tableSize - 1)
	for i := 0; i < 128; i++ {
		k := keys[h].Load()
		if k == key {
			counts[h].Add(1)
			return
		}
		if k == 0 {
			if keys[h].CompareAndSwap(0, key) {
				counts[h].Add(1)
				noteFid(fid, self)
				return
			}
			i--
			continue
		}
		h = (h + 1) & (tableSize - 1)
	}
	slowAdd(fid, self, pc)
}

const calibFid = 1<<24 - 1

// calibrate finds where E keeps its saved frame pointer relative to its local
// anchor, by calling E with calibFid from a known frame.
//
//go:noinline
func calibrate() {
	enabled.Store(true)
	E(calibFid)
	enabled.Store(false)
}

//go:noinline
func calibrateIn(a uintptr) {
	var pcs [2]uintptr
	// skip 3: runtime.Callers, calibrateIn, E -> pcs[0] return PC into
	// calibrate (held in E's return-address slot), pcs[1] return PC into
	// calibrate's caller (held one word above calibrate's saved BP)
	if runtime.Callers(3, pcs[:]) < 2 {
		return
	}
	for i := uintptr(1); i < 64; i++ {
		if *(*uintptr)(unsafe.Pointer(a + 8*i)) == pcs[0] {
			off := 8*i - 8
			fbp := *(*uintptr)(unsafe.Pointer(a + off))
			if *(*uintptr)(unsafe.Pointer(fbp + 8)) == pcs[1] {
				bpOff = off
				calibPC = pcs
				calibrated.Store(true)
			}
			return
		}
	}
}

func hash(k uint64) uint64 {
	k ^= k >> 33
	k *= 0xff51afd7ed558ccd
	k ^= k >> 33
	return k
}

func noteFid(fid uint32, self uintptr) {
	fidMu.Lock()
	if _, ok := fidPC[fid]; !ok {
		fidPC[fid] = self
	}
	fidMu.Unlock()
}

func slowAdd(fid uint32, self, pc uintptr) {
	noteFid(fid, self)
	k := uint64(fid)<<32 ^ uint64(pc)
	slowMu.Lock()
	slow[k]++
	slowPC[k] = [2]uint64{uint64(fid), uint64(pc)}
	slowMu.Unlock()
}

// goEntry handles a function that is the first frame of a goroutine. Its
// logical caller is runtime.goexit, so the go statement is taken from the
// "created by" lines of runtime.Stack.
func goEntry(fid uint32, self uintptr) {
	noteFid(fid, self)
	var buf [2048]byte
	n := runtime.Stack(buf[:], false)
	fn, file, line := parseCreatedBy(buf[:n])
	goMu.Lock()
	goHits[goKey{fid, fn, file, line}]++
	goMu.Unlock()
}

func parseCreatedBy(b []byte) (fn, file string, line int) {
	i := bytes.LastIndex(b, []byte("\ncreated by "))
	if i < 0 {
		return "?", "?", 0
	}
	rest := b[i+len("\ncreated by "):]
	nl := bytes.IndexByte(rest, '\n')
	if nl < 0 {
		return "?", "?", 0
	}
	fn = string(rest[:nl])
	if j := strings.Index(fn, " in goroutine "); j >= 0 {
		fn = fn[:j]
	}
	loc := rest[nl+1:]
	loc = bytes.TrimLeft(loc, "\t")
	if e := bytes.IndexByte(loc, '\n'); e >= 0 {
		loc = loc[:e]
	}
	if sp := bytes.LastIndex(loc, []byte(" +0x")); sp >= 0 {
		loc = loc[:sp]
	}
	c := bytes.LastIndexByte(loc, ':')
	if c < 0 {
		return fn, string(loc), 0
	}
	line, _ = strconv.Atoi(string(loc[c+1:]))
	return fn, string(loc[:c]), line
}

type exitHook struct {
	F            func()
	RunOnFailure bool
}

//go:linkname exithookAdd internal/runtime/exithook.Add
func exithookAdd(h exitHook)

func init() {
	ch := make(chan uintptr)
	go func() {
		var pcs [16]uintptr
		n := runtime.Callers(0, pcs[:])
		// the last frame of every goroutine is runtime.goexit; every goroutine
		// returns to the same PC there
		if n > 0 {
			if f := runtime.FuncForPC(pcs[n-1] - 1); f != nil && f.Name() == "runtime.goexit" {
				ch <- pcs[n-1]
				return
			}
		}
		ch <- 0
	}()
	goexitPC = <-ch
	if runtime.GOARCH == "amd64" && os.Getenv("GORACLE_SLOW") == "" {
		calibrate()
		fastOK = calibrated.Load()
	}
	if os.Getenv("GORACLE_OUT") == "" {
		return
	}
	exithookAdd(exitHook{F: Flush, RunOnFailure: true})
	if ms, _ := strconv.Atoi(os.Getenv("GORACLE_SNAPSHOT_MS")); ms > 0 {
		go func() {
			for {
				time.Sleep(time.Duration(ms) * time.Millisecond)
				write(true)
			}
		}()
	}
	enabled.Store(true)
}

// Flush writes the records. It runs from the exit hook; tests and mains that
// end through a fatal signal or an unrecovered panic lose their records unless
// GORACLE_SNAPSHOT_MS is set.
func Flush() {
	if flushed.Swap(true) {
		return
	}
	enabled.Store(false)
	write(false)
}

type rec struct {
	fid   uint32
	pc    uintptr
	count uint64
}

func write(snapshot bool) {
	flushMu.Lock()
	defer flushMu.Unlock()
	dir := os.Getenv("GORACLE_OUT")
	var recs []rec
	for i := range keys {
		k := keys[i].Load()
		if k == 0 {
			continue
		}
		c := counts[i].Load()
		if c&indirect != 0 {
			continue // pair whose fast-path PC was a wrapper; its hits are recorded under the logical caller
		}
		recs = append(recs, rec{uint32(k>>pcBits) - 1, uintptr(k & (1<<pcBits - 1)), c})
	}
	slowMu.Lock()
	for k, c := range slow {
		v := slowPC[k]
		recs = append(recs, rec{uint32(v[0]), uintptr(v[1]), c})
	}
	slowMu.Unlock()
	fidMu.Lock()
	names := make(map[uint32]string, len(fidPC))
	for fid, pc := range fidPC {
		f, _ := runtime.CallersFrames([]uintptr{pc}).Next()
		names[fid] = f.Function
	}
	fidMu.Unlock()
	goMu.Lock()
	gos := make(map[goKey]uint64, len(goHits))
	for k, v := range goHits {
		gos[k] = v
	}
	goMu.Unlock()

	name := "hits-" + strconv.Itoa(os.Getpid()) + ".jsonl"
	if snapshot {
		name = "snap-" + strconv.Itoa(os.Getpid()) + ".jsonl"
	}
	tmp := filepath.Join(dir, name+".tmp"+strconv.FormatUint(seq.Add(1), 10))
	out, err := os.Create(tmp)
	if err != nil {
		return
	}
	w := bufio.NewWriter(out)
	exe, _ := os.Executable()
	w.WriteString(`{"kind":"meta","pid":` + strconv.Itoa(os.Getpid()) + `,"exe":` + strconv.Quote(exe) +
		`,"args":` + strconv.Quote(strings.Join(os.Args[1:], " ")) + `,"snapshot":` + strconv.FormatBool(snapshot) +
		`,"fast":` + strconv.FormatBool(fastOK) + `,"bp_off":` + strconv.FormatUint(uint64(bpOff), 10) +
		`,"fast_checks":` + strconv.FormatUint(fastChecks.Load(), 10) +
		`,"fast_differ":` + strconv.FormatUint(fastDiffer.Load(), 10) + "}\n")
	frameCache := map[uintptr]runtime.Frame{}
	for _, r := range recs {
		f, ok := frameCache[r.pc]
		if !ok {
			f, _ = runtime.CallersFrames([]uintptr{r.pc}).Next()
			frameCache[r.pc] = f
		}
		w.WriteString(`{"kind":"call","fid":` + strconv.FormatUint(uint64(r.fid), 10) +
			`,"callee":` + strconv.Quote(names[r.fid]) +
			`,"caller":` + strconv.Quote(f.Function) + `,"file":` + strconv.Quote(f.File) +
			`,"line":` + strconv.Itoa(f.Line) + `,"pc":` + strconv.FormatUint(uint64(r.pc), 10) +
			`,"n":` + strconv.FormatUint(r.count, 10) + "}\n")
	}
	for i := range doneCounts {
		if c := doneCounts[i].Load(); c != 0 {
			w.WriteString(`{"kind":"done","fid":` + strconv.Itoa(i) + `,"n":` + strconv.FormatUint(c, 10) + "}\n")
		}
	}
	for k, c := range gos {
		w.WriteString(`{"kind":"go","fid":` + strconv.FormatUint(uint64(k.fid), 10) +
			`,"callee":` + strconv.Quote(names[k.fid]) +
			`,"caller":` + strconv.Quote(k.createdBy) + `,"file":` + strconv.Quote(k.file) +
			`,"line":` + strconv.Itoa(k.line) + `,"n":` + strconv.FormatUint(c, 10) + "}\n")
	}
	if w.Flush() == nil && out.Close() == nil {
		os.Rename(tmp, filepath.Join(dir, name))
	} else {
		os.Remove(tmp)
	}
}
