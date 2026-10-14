// Package rt is the runtime half of goracle. The instrumenter makes every
// function body and func literal in the copied tree start with E(fid). E records
// the pair (fid, return PC of the caller's call instruction) and counts it. At
// process exit the pairs are resolved to frames and written to
// $GORACLE_OUT/hits-<pid>.jsonl. D(id) counts one execution of an instrumented
// statement (a completion counter after a simple statement, or a reach
// counter before a return or control statement). Standard library only.
//
// Build the instrumented tree with -gcflags=all=-l -ldflags=-checklinkname=0:
// the exit hook is reached through a linkname to internal/runtime/exithook
// (exit.go.in, written as exit.go into instrumented copies only).
// Pinned to Go 1.27: the frame layout, the "created by" format of
// runtime.Stack and the exithook linkname are toolchain internals.
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
	slow   = map[slowKey]uint64{} // table overflow or pc >= 1<<pcBits

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

type slowKey struct {
	fid uint32
	pc  uintptr
}

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
	var fbp unsafe.Pointer // frame pointer of the instrumented function; nil before calibration
	var pc uintptr         // its physical return PC
	if calibrated.Load() {
		// the saved BP is read as a pointer (it points into this goroutine's
		// stack) so that no uintptr is converted back to a pointer
		fbp = *(*unsafe.Pointer)(unsafe.Add(unsafe.Pointer(&anchor), bpOff))
		pc = *(*uintptr)(unsafe.Add(fbp, 8))
	}
	if !fastOK {
		if fid == calibFid {
			calibrateIn(unsafe.Pointer(&anchor))
			return
		}
		slowEnter(fid, fbp)
		return
	}
	if pc == goexitPC || uint64(pc) >= 1<<pcBits {
		slowEnter(fid, fbp)
		return
	}
	key := uint64(fid+1)<<pcBits | uint64(pc)
	h := hash(key) & (tableSize - 1)
	for i := 0; i < 128; i++ {
		k := keys[h].Load()
		if k == key {
			if counts[h].Add(1)&indirect != 0 {
				slowEnter(fid, fbp)
			}
			return
		}
		if k == key|pending {
			slowEnter(fid, fbp) // another goroutine is still checking this pair
			return
		}
		if k == 0 {
			if keys[h].CompareAndSwap(0, key|pending) {
				firstSeen(fid, fbp, pc, h, key)
				return
			}
			i--
			continue // somebody filled this slot; look at it again
		}
		h = (h + 1) & (tableSize - 1)
	}
	slowEnter(fid, fbp)
}

const (
	indirect = 1 << 63 // in counts: the pair's frame-pointer PC was a wrapper
	pending  = 1 << 63 // in keys: the pair is being checked against runtime.Callers
)

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

// firstSeen checks a new fast-path pair against runtime.Callers. While it
// runs the slot's key carries the pending bit, so concurrent hits on the pair
// take the slow path instead of being counted under a PC that may turn out to
// be a wrapper.
//
//go:noinline
func firstSeen(fid uint32, fbp unsafe.Pointer, pc uintptr, slot, key uint64) {
	var pcs [2]uintptr
	// skip 3: runtime.Callers, firstSeen, E
	n := runtime.Callers(3, pcs[:])
	fastChecks.Add(1)
	if n == 2 && pcs[1] == pc && physKind(pc) != physPanic {
		counts[slot].Add(1)
		noteFid(fid, pcs[0])
		keys[slot].Store(key)
		return
	}
	// a wrapper, an inlined callee, or an entry that runtime.gopanic or
	// runtime.Goexit runs (each of those takes the slow path to record its
	// stack): every hit of the pair goes through runtime.Callers
	fastDiffer.Add(1)
	counts[slot].Add(indirect)
	keys[slot].Store(key)
	if n == 2 {
		noteFid(fid, pcs[0]) // record may count under this slot, which write skips
		noteDeferreturn(fbp, pcs[1])
		notePanic(fid, fbp, pcs[1])
		record(fid, pcs[0], pcs[1])
	}
}

//go:noinline
func slowEnter(fid uint32, fbp unsafe.Pointer) {
	var pcs [2]uintptr
	// skip 3: runtime.Callers, slowEnter, E -> pcs[0] is the instrumented
	// function, pcs[1] the logical caller. Wrapper frames (method wrappers,
	// -fm method values, go/defer wrappers, runtime.deferreturn) are elided
	// by runtime.Callers.
	if runtime.Callers(3, pcs[:]) < 2 {
		return
	}
	noteDeferreturn(fbp, pcs[1])
	notePanic(fid, fbp, pcs[1])
	record(fid, pcs[0], pcs[1])
}

const (
	physOther = iota
	physDeferreturn
	physDeferwrap
	physPanic
)

var (
	physKinds sync.Map // PC -> physOther, physDeferreturn, physDeferwrap or physPanic
	drLogical sync.Map // logical caller PC -> true: reached through runtime.deferreturn
	panicMu   sync.Mutex
	panicHits = map[panicKey]uint64{}
)

type panicKey struct {
	fid   uint32
	stack string // logical frames above runtime.gopanic / runtime.Goexit, newline-separated
	owner string // the deferwrap closure that ran the entry (its parent made the defer), "" if none
}

// physKind classifies the function a return PC lies in.
func physKind(pc uintptr) int {
	if pc == 0 {
		return physOther
	}
	if v, ok := physKinds.Load(pc); ok {
		return v.(int)
	}
	kind := physOther
	if f := runtime.FuncForPC(pc - 1); f != nil {
		switch name := f.Name(); {
		case name == "runtime.deferreturn":
			kind = physDeferreturn
		case name == "runtime.gopanic" || name == "runtime.Goexit":
			kind = physPanic
		case strings.Contains(name, ".deferwrap"):
			kind = physDeferwrap
		}
	}
	physKinds.Store(pc, kind)
	return kind
}

// noteDeferreturn marks the logical caller PC of a deferred call that
// runtime.deferreturn ran. runtime.Callers elides deferreturn and the
// wrappers between it and the deferred function (the compiler's deferwrap
// closures for defer f(x) and defer x.M(), method wrappers behind an
// interface, promoted-method wrappers, -fm method values), so such a call
// looks like a call from the deferring function at the position of its
// deferreturn call, which the compiler can give the position of a tagged call
// site (after a recovered panic in a function without a return). The
// frame-pointer chain tells them apart: walking up from the instrumented
// function through frames with a saved BP, a deferred call reaches a return
// PC in runtime.deferreturn before the logical caller's.
func noteDeferreturn(fbp unsafe.Pointer, logical uintptr) {
	bp := fbp
	for i := 0; i < 8 && bp != nil; i++ {
		ret := *(*uintptr)(unsafe.Add(bp, 8)) // return PC into the next frame up
		if ret == logical {
			return
		}
		switch physKind(ret) {
		case physDeferreturn:
			if _, seen := drLogical.Load(logical); !seen {
				drLogical.Store(logical, true)
			}
			return
		case physPanic:
			return
		}
		bp = *(*unsafe.Pointer)(bp) // that frame's own frame pointer
	}
}

// notePanic records the logical stack above runtime.gopanic or
// runtime.Goexit for an entry they run (a deferred call during a panic or
// Goexit): report credits it to the functions with defers on that stack. When
// the frame-pointer chain shows the compiler's deferwrap closure of the
// deferred call (defer f(x), defer x.M()), its name says which function made
// the defer, and only that one is credited.
func notePanic(fid uint32, fbp unsafe.Pointer, logical uintptr) {
	if physKind(logical) != physPanic {
		return
	}
	owner := ""
	bp := fbp
	for i := 0; i < 8 && bp != nil; i++ {
		ret := *(*uintptr)(unsafe.Add(bp, 8))
		kind := physKind(ret)
		if kind == physDeferwrap {
			if f := runtime.FuncForPC(ret - 1); f != nil {
				owner = f.Name()
			}
		}
		if kind != physOther {
			break
		}
		bp = *(*unsafe.Pointer)(bp)
	}
	var pcs [48]uintptr
	// skip 4: runtime.Callers, notePanic, firstSeen or slowEnter, E
	n := runtime.Callers(4, pcs[:])
	frames := runtime.CallersFrames(pcs[:n])
	var names []string
	above := false
	for {
		f, more := frames.Next()
		if above && !strings.HasPrefix(f.Function, "runtime.") && f.Function != "" {
			names = append(names, f.Function)
		}
		if f.Function == "runtime.gopanic" || f.Function == "runtime.Goexit" {
			above = true
		}
		if !more || len(names) >= 32 {
			break
		}
	}
	panicMu.Lock()
	panicHits[panicKey{fid, strings.Join(names, "\n"), owner}]++
	panicMu.Unlock()
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
func calibrateIn(a unsafe.Pointer) {
	var pcs [2]uintptr
	// skip 3: runtime.Callers, calibrateIn, E -> pcs[0] return PC into
	// calibrate (held in E's return-address slot), pcs[1] return PC into
	// calibrate's caller (held one word above calibrate's saved BP)
	if runtime.Callers(3, pcs[:]) < 2 {
		return
	}
	for i := uintptr(1); i < 64; i++ {
		if *(*uintptr)(unsafe.Add(a, 8*i)) == pcs[0] {
			off := 8*i - 8
			fbp := *(*unsafe.Pointer)(unsafe.Add(a, off))
			if *(*uintptr)(unsafe.Add(fbp, 8)) == pcs[1] {
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
	slowMu.Lock()
	slow[slowKey{fid, pc}]++
	slowMu.Unlock()
}

// goEntry handles a function that is the first frame of a goroutine. Its
// logical caller is runtime.goexit, so the go statement is taken from the
// "created by" lines of runtime.Stack.
func goEntry(fid uint32, self uintptr) {
	noteFid(fid, self)
	buf := make([]byte, 8192)
	n := runtime.Stack(buf, false)
	for n == len(buf) && len(buf) < 1<<20 {
		buf = make([]byte, 4*len(buf))
		n = runtime.Stack(buf, false)
	}
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

// registerExit runs f at process exit, also after os.Exit with a non-zero
// code. exit.go, which goracle writes next to this file in an instrumented
// copy, sets it from a package-level initializer (so before init runs)
// through a linkname to internal/runtime/exithook.Add; that linkname needs
// -ldflags=-checklinkname=0. Without it nothing is recorded.
var registerExit func(f func())

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
	if runtime.GOARCH == "amd64" {
		calibrate()
		fastOK = calibrated.Load() && os.Getenv("GORACLE_SLOW") == ""
	}
	if os.Getenv("GORACLE_OUT") == "" || registerExit == nil {
		return
	}
	registerExit(Flush)
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
	// counters first: a statement completes after its calls entered, so
	// reading the counters before the entries never shows exec > hits
	type done struct {
		id int
		n  uint64
	}
	var dones []done
	for i := range doneCounts {
		if c := doneCounts[i].Load(); c != 0 {
			dones = append(dones, done{i, c})
		}
	}
	var recs []rec
	for i := range keys {
		k := keys[i].Load() &^ pending
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
		recs = append(recs, rec{k.fid, k.pc, c})
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
	panicMu.Lock()
	panics := make(map[panicKey]uint64, len(panicHits))
	for k, v := range panicHits {
		panics[k] = v
	}
	panicMu.Unlock()

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
		deferred := ""
		if _, ok := drLogical.Load(r.pc); ok {
			deferred = `,"defer":true`
		}
		w.WriteString(`{"kind":"call","fid":` + strconv.FormatUint(uint64(r.fid), 10) +
			`,"callee":` + strconv.Quote(names[r.fid]) +
			`,"caller":` + strconv.Quote(f.Function) + `,"file":` + strconv.Quote(f.File) +
			`,"line":` + strconv.Itoa(f.Line) + `,"pc":` + strconv.FormatUint(uint64(r.pc), 10) +
			`,"n":` + strconv.FormatUint(r.count, 10) + deferred + "}\n")
	}
	for _, d := range dones {
		w.WriteString(`{"kind":"done","fid":` + strconv.Itoa(d.id) + `,"n":` + strconv.FormatUint(d.n, 10) + "}\n")
	}
	for k, c := range gos {
		w.WriteString(`{"kind":"go","fid":` + strconv.FormatUint(uint64(k.fid), 10) +
			`,"callee":` + strconv.Quote(names[k.fid]) +
			`,"caller":` + strconv.Quote(k.createdBy) + `,"file":` + strconv.Quote(k.file) +
			`,"line":` + strconv.Itoa(k.line) + `,"n":` + strconv.FormatUint(c, 10) + "}\n")
	}
	for k, c := range panics {
		var stack []string
		if k.stack != "" {
			stack = strings.Split(k.stack, "\n")
		}
		quoted := make([]string, len(stack))
		for i, name := range stack {
			quoted[i] = strconv.Quote(name)
		}
		owner := ""
		if k.owner != "" {
			owner = `,"owner":` + strconv.Quote(k.owner)
		}
		w.WriteString(`{"kind":"panic","fid":` + strconv.FormatUint(uint64(k.fid), 10) +
			`,"callee":` + strconv.Quote(names[k.fid]) + `,"stack":[` + strings.Join(quoted, ",") + `]` + owner +
			`,"n":` + strconv.FormatUint(c, 10) + "}\n")
	}
	if w.Flush() == nil && out.Close() == nil {
		os.Rename(tmp, filepath.Join(dir, name))
	} else {
		os.Remove(tmp)
	}
}
