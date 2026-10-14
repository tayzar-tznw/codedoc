"""Sidecar table of probe sites (engine-probe, 2026-10-13).

Each site is located by its marker comment `//@ID` (file found by scanning) and
by `token` (the callee token on that line; `occ` picks the n-th occurrence).
`expect` lists the runtime-correct target(s) as runtime-style names (the form
runtime.Frame.Function prints, with "[...]" removed), derived from Go
semantics and confirmed by the trace oracle (lib/trace) where `oracle` is
"trace". Special forms:
  conv:<type>     the expression is a conversion, not a call
  builtin:<name>  a predeclared function (or unsafe.*)
  none            no call happens at this token
  loopbody@<ID>   the range-over-func loop body of site <ID>
  c:<name>        a C function (cgo preamble)
  asm:<qname>     body implemented in assembly
`alt` lists targets that are correct only in another build (GOOS, tags, cgo,
test binary). `oracle`: trace | dyn (dynamic type recorded) | spec (Go spec;
no Go body to trace) | unexecuted (path not run; static reasoning only).
"""

L = "example.com/lib"
E = "example.com/lib/embed"
I = "example.com/lib/iface"
G = "example.com/lib/gen"
T = "example.com/lib/testy"

SITES = [
    # --- package-level calls
    dict(id="PK01", token="localFunc", expect=["main.localFunc"], cat="pkg", oracle="trace", note="same-package function"),
    dict(id="PK02", token="Hello", expect=[f"{L}.Hello"], cat="pkg", oracle="trace", note="qualified call into another module via go.work use"),
    dict(id="PK03", token="Hello", expect=[f"{L}.Hello"], cat="pkg", oracle="trace", note="import alias L"),
    dict(id="PK04", token="Dotted", expect=["example.com/lib/dotpkg.Dotted"], cat="pkg", oracle="trace", note="dot import"),
    dict(id="PK05", token="Help", expect=["example.com/app/internal/helper.Help"], cat="pkg", oracle="trace", note="same module, other package"),
    dict(id="PK06", token="blankpkg", expect=["none"], cat="pkg", oracle="trace", note="blank import; its init runs at program start (caller runtime.doInit1)"),
    dict(id="IN04", token="Hello", expect=[f"{L}.Hello"], cat="pkg", oracle="trace", note="call inside func init() (runtime name blankpkg.init.0)"),
    # --- methods
    dict(id="ME01", token="Value", expect=[f"{L}.Counter.Value"], cat="method", oracle="trace", note="value receiver on value"),
    dict(id="ME02", token="Inc", expect=[f"{L}.(*Counter).Inc"], cat="method", oracle="trace", note="pointer receiver on addressable value"),
    dict(id="ME03", token="NewCounter", expect=[f"{L}.NewCounter"], cat="method", oracle="trace", note="constructor-style function"),
    dict(id="ME04", token="Value", expect=[f"{L}.Counter.Value"], cat="method", oracle="trace", note="value method via pointer"),
    dict(id="ME05", token="Inc", expect=[f"{L}.(*Counter).Inc"], cat="method", oracle="trace", note="pointer method via pointer"),
    dict(id="ME06", token="F", expect=[f"{L}.Celsius.F"], cat="method", oracle="trace", note="method on defined float64 type"),
    dict(id="ME07", token="Len", expect=[f"{L}.Names.Len"], cat="method", oracle="trace", note="method on defined slice type, composite literal receiver"),
    dict(id="ME08", token="Value", expect=[f"{L}.Counter.Value"], cat="method", oracle="trace", note="method expression T.M"),
    dict(id="ME09", token="Inc", expect=[f"{L}.(*Counter).Inc"], cat="method", oracle="trace", note="method expression (*T).M"),
    dict(id="ME10", token="Value", expect=[f"{L}.Counter.Value"], cat="method", oracle="trace", note="method expression (*T).M of a value method (wrapper)"),
    dict(id="ME11", token="Value", expect=["none"], cat="method", oracle="trace", note="method value created, not called"),
    dict(id="ME12", token="f", expect=[f"{L}.Counter.Value"], cat="method", oracle="trace", note="call of method value f := c.Value"),
    dict(id="ME13", token="g", expect=[f"{L}.(*Counter).Inc"], cat="method", oracle="trace", note="call of method value g := p.Inc"),
    dict(id="ME14", token="apply", expect=["main.apply"], cat="method", oracle="trace", note="call with bound method argument"),
    dict(id="ME14b", marker="ME14", token="Inc", expect=["none"], cat="method", oracle="trace", note="bound method passed as argument (reference only)"),
    dict(id="ME15", token="fn", expect=[f"{L}.(*Counter).Inc"], cat="method", oracle="trace", note="call of func parameter holding a bound method"),
    dict(id="CV01", token="Celsius", expect=[f"conv:{L}.Celsius"], cat="notcall", oracle="spec", note="conversion to defined type"),
    # --- embedding
    dict(id="EM01", token="Hello", expect=[f"{E}.Base.Hello"], cat="embed", oracle="trace", note="promoted, depth 1"),
    dict(id="EM02", token="Hello", expect=[f"{E}.Base.Hello"], cat="embed", oracle="trace", note="promoted, depth 2"),
    dict(id="EM03", token="Shadowed", expect=[f"{E}.Mid.Shadowed"], cat="embed", oracle="trace", note="depth-1 Mid.Shadowed shadows depth-2 Base.Shadowed"),
    dict(id="EM04", token="PtrM", expect=[f"{E}.(*Base).PtrM"], cat="embed", oracle="trace", note="pointer-embedded *Base, pointer method"),
    dict(id="EM05", token="Hello", expect=[f"{E}.Base.Hello"], cat="embed", oracle="trace", note="pointer-embedded *Base, value method"),
    dict(id="EM06", token="Greet", expect=[f"{E}.English.Greet"], cat="embed", oracle="trace", note="struct embeds interface Greeter (dynamic)"),
    dict(id="EM07", token="Read", expect=[f"{E}.file.Read"], cat="embed", oracle="trace", note="interface embeds interface (Reader in ReadCloser)"),
    dict(id="EM08", token="Close", expect=[f"{E}.file.Close"], cat="embed", oracle="trace", note="method declared in the outer interface"),
    dict(id="EM09", token="Hello", expect=[f"{E}.Base.Hello"], cat="embed", oracle="trace", note="explicit selector path t.Mid.Base.Hello"),
    dict(id="EM10", token="Shadowed", expect=[f"{E}.Base.Shadowed"], cat="embed", oracle="trace", note="explicit path to the shadowed method"),
    # --- interfaces
    dict(id="IF01", token="Nothing", expect=["none"], cat="iface", oracle="unexecuted", note="0 in-repo implementers; nil, never runs"),
    dict(id="IF02", token="Only", expect=[f"{I}.OnlyImpl.Only"], cat="iface", oracle="trace", note="1 in-repo implementer"),
    dict(id="IF03", token="Do", expect=[f"{I}.A.Do", f"{I}.(*B).Do"], cat="iface", oracle="trace", note="2 in-repo implementers, both run"),
    dict(id="IF04", token="Printf", expect=["log.(*Logger).Printf"], cat="iface", oracle="dyn", note="in-repo Logger satisfied by *log.Logger"),
    dict(id="IF05", token="Printf", expect=[f"{I}.MyLog.Printf"], cat="iface", oracle="trace", note="same in-repo Logger, in-repo value"),
    dict(id="IF06", token="Write", expect=[f"{I}.(*Buf).Write"], cat="iface", oracle="trace", note="stdlib io.Writer on in-repo value"),
    dict(id="IF07", token="Fprintf", expect=["fmt.Fprintf"], cat="iface", oracle="spec", note="stdlib function; it calls (*Buf).Write internally (trace: caller fmt/print.go)"),
    dict(id="IF08", token="Name", expect=[f"{I}.S1.Name"], cat="iface", oracle="trace", note="sealed interface (unexported method)"),
    dict(id="IF09", token="Name", expect=[f"{I}.S1.Name"], cat="iface", oracle="unexecuted", note="type-switch case S1 (static type S1)"),
    dict(id="IF10", token="Name", expect=[f"{I}.S2.Name"], cat="iface", oracle="trace", note="type-switch case S2"),
    dict(id="IF11", token="Name", expect=[f"{I}.S2.Name"], cat="iface", oracle="trace", note="assertion to interface, then call"),
    dict(id="IF12", token="Name", expect=[f"{I}.S2.Name"], cat="iface", oracle="trace", note="assertion to concrete type, then call"),
    dict(id="IF13", token="Do", expect=[f"{I}.A.Do"], cat="iface", oracle="trace", note="method expression on an interface type Multi.Do(x)"),
    dict(id="IF14", token="md", expect=[f"{I}.S1.Name"], cat="iface", oracle="trace", note="method value taken from an interface value, then called"),
    dict(id="IF15", token="Name", expect=[f"{I}.S2.Name"], cat="iface", oracle="trace", note="assertion to an unnamed interface literal, then call"),
    # --- generics
    dict(id="GE01", token="Identity", expect=[f"{G}.Identity"], cat="generic", oracle="trace", note="explicit instantiation"),
    dict(id="GE02", token="Identity", expect=[f"{G}.Identity"], cat="generic", oracle="trace", note="inferred instantiation"),
    dict(id="GE03", token="Map", expect=[f"{G}.Map"], cat="generic", oracle="trace", note="inferred, func-valued argument"),
    dict(id="GE16", token="f", expect=["strconv.Itoa"], cat="generic", oracle="spec", note="call of func param inside generic Map (value from GE03)"),
    dict(id="GE04", token="Push", expect=[f"{G}.(*List).Push"], cat="generic", oracle="trace", note="method of generic type"),
    dict(id="GE05", token="Push", expect=[f"{G}.(*List).Push"], cat="generic", oracle="trace", note="method through generic type alias Alias[T] = List[T]"),
    dict(id="GE06", token="Describe", expect=[f"{G}.Describe"], cat="generic", oracle="trace", note="generic func with method constraint"),
    dict(id="GE07", token="Describe", expect=[f"{G}.Describe"], cat="generic", oracle="trace", note="same, other type argument"),
    dict(id="GE13", token="String", expect=[f"{G}.Named.String", f"{G}.Other.String"], cat="generic", oracle="trace", note="call through type-parameter constraint method"),
    dict(id="GE08", token="Count", expect=[f"{G}.Count"], cat="generic", oracle="trace", note="range-over-func: explicit call that returns the iterator"),
    dict(id="GE14", token="yield", expect=["loopbody@GE08"], cat="generic", oracle="trace", note="yield call inside iterator; runtime target main.generics-range1"),
    dict(id="GE09", token="All", expect=[f"{G}.(*Bag).All"], cat="generic", oracle="trace", note="range over method value: implicit call, no call syntax"),
    dict(id="GE15", token="yield", expect=["loopbody@GE09"], cat="generic", oracle="unexecuted", note="yield inside method iterator (bag empty)"),
    dict(id="GE10", token="Apply", expect=[f"{G}.Box.Apply"], cat="generic", oracle="trace", note="Go 1.27 generic method, inferred F"),
    dict(id="GE17", token="f", expect=["strconv.Itoa"], cat="generic", oracle="spec", note="func param call inside generic method"),
    dict(id="GE12", token="idf", expect=[f"{G}.Identity"], cat="generic", oracle="trace", note="Go 1.27: generic func assigned to typed var, inferred; then called"),
    dict(id="GE18", token="Sum", expect=[f"{G}.Sum"], cat="generic", oracle="trace", note="generic func with type-set constraint"),
    dict(id="GE19", token="Push", expect=[f"{G}.(*List).Push"], cat="generic", oracle="trace", note="method promoted from embedded generic type Wrapped[T]{List[T]}"),
    # --- function values
    dict(id="FV01", token="fnVar", expect=[f"{L}.Hello"], cat="funcval", oracle="trace", note="package-level func var"),
    dict(id="FV02", token="fn", expect=[f"{L}.Hello"], cat="funcval", oracle="trace", note="struct field"),
    dict(id="FV03", token="f", expect=["main.localHello"], cat="funcval", oracle="trace", note="parameter"),
    dict(id="FV04", token="handlers", expect=[f"{L}.Hello"], cat="funcval", oracle="trace", note="map of funcs, literal key"),
    dict(id="FV05", token="fs", expect=["main.localHello"], cat="funcval", oracle="trace", note="slice of funcs"),
    dict(id="FV06", token="cl", expect=["main.funcValues.func@42"], cat="funcval", oracle="trace", note="closure assigned then called (runtime main.funcValues.func1)"),
    dict(id="FV07", token="func", expect=["main.funcValues.func@47"], cat="funcval", oracle="trace", note="immediately invoked func literal (query on func keyword)"),
    dict(id="FV07p", marker="FV07", token="()", occ=3, expect=["main.funcValues.func@47"], cat="funcval", oracle="trace", note="same IIFE, query on the call paren"),
    dict(id="FV08", token="localDefer", expect=["main.localDefer"], cat="funcval", oracle="trace", note="defer f(); runs at function exit (trace line = closing brace)"),
    dict(id="FV09", token="func", expect=["main.funcValues.func@49"], cat="funcval", oracle="trace", note="defer func(){}()"),
    dict(id="FV10", token="goTarget", expect=["main.goTarget"], cat="funcval", oracle="trace", note="go f(); trace caller is runtime.goexit"),
    dict(id="FV11", token="func", expect=["main.funcValues.func@53"], cat="funcval", oracle="trace", note="go func(){}()"),
    dict(id="FV12", token="op", expect=["main.localHello"], cat="funcval", oracle="trace", note="local var bound twice (lib.Hello, then localHello)"),
    # --- not calls
    dict(id="NC01", token="Hello", expect=[f"{L}.Hello"], cat="notcall", oracle="trace", note="package-level var initializer; runtime caller main.init (no source function)"),
    dict(id="NC02", token="Hello", expect=[f"{L}.Hello"], cat="notcall", oracle="trace", note="inside first func init() (runtime main.init.0)"),
    dict(id="NC03", token="localHello", expect=["main.localHello"], cat="notcall", oracle="trace", note="inside second func init() in same file (runtime main.init.1)"),
    dict(id="NC04", token="int32", expect=["conv:int32"], cat="notcall", oracle="spec", note="conversion to basic type"),
    dict(id="NC05", token="Counter", occ=1, expect=[f"conv:{L}.Counter"], cat="notcall", oracle="spec", note="conversion (*lib.Counter)(p)"),
    dict(id="NC06", token="byte", expect=["conv:[]byte"], cat="notcall", oracle="spec", note="conversion []byte(s)"),
    dict(id="NC07", token="MyFunc", expect=["conv:main.MyFunc"], cat="notcall", oracle="spec", note="func-type conversion MyFunc(f)"),
    dict(id="NC08", token="mf", expect=["main.localHello"], cat="notcall", oracle="trace", note="call through the converted func value"),
    dict(id="NC09", token="Celsius", expect=[f"conv:{L}.Celsius"], cat="notcall", oracle="spec", note="conversion to type in other package"),
    dict(id="NC10", token="List", occ=1, expect=[f"conv:{G}.List"], cat="notcall", oracle="spec", note="conversion to instantiated generic type"),
    dict(id="NC11", token="func", expect=["conv:func() string"], cat="notcall", oracle="spec", note="conversion to func type literal"),
    dict(id="BI01", token="len", expect=["builtin:len"], cat="builtin", oracle="spec"),
    dict(id="BI02", token="append", expect=["builtin:append"], cat="builtin", oracle="spec"),
    dict(id="BI03", token="make", expect=["builtin:make"], cat="builtin", oracle="spec"),
    dict(id="BI04", token="new", expect=["builtin:new"], cat="builtin", oracle="spec"),
    dict(id="BI05", token="min", expect=["builtin:min"], cat="builtin", oracle="spec"),
    dict(id="BI06", token="max", expect=["builtin:max"], cat="builtin", oracle="spec"),
    dict(id="BI07", token="clear", expect=["builtin:clear"], cat="builtin", oracle="spec"),
    dict(id="BI08", token="recover", expect=["builtin:recover"], cat="builtin", oracle="spec"),
    dict(id="BI09", token="panic", expect=["builtin:panic"], cat="builtin", oracle="spec"),
    dict(id="BI10", token="Sizeof", expect=["builtin:unsafe.Sizeof"], cat="builtin", oracle="spec"),
    # --- stdlib and third party
    dict(id="TP01", token="New", expect=["github.com/google/uuid.New"], cat="external", oracle="spec"),
    dict(id="TP02", token="Must", expect=["github.com/google/uuid.Must"], cat="external", oracle="spec"),
    dict(id="TP02b", marker="TP02", token="NewRandom", expect=["github.com/google/uuid.NewRandom"], cat="external", oracle="spec"),
    dict(id="TP03", token="String", expect=["github.com/google/uuid.UUID.String"], cat="external", oracle="spec"),
    dict(id="SL01", token="ToUpper", expect=["strings.ToUpper"], cat="external", oracle="spec"),
    dict(id="SL02", token="WriteString", expect=["strings.(*Builder).WriteString"], cat="external", oracle="spec"),
    # --- build constraints
    dict(id="BC01", token="platformName", expect=["main.platformName"], cat="build", oracle="trace", note="linux: plat_linux.go; alt windows/darwin files",
         expect_file="app/plat_linux.go", alt=["app/plat_windows.go", "app/plat_darwin.go"]),
    dict(id="BC10", token="linuxOnly", expect=["main.linuxOnly"], cat="build", oracle="trace", note="call inside plat_linux.go"),
    dict(id="BC02", token="winOnly", expect=["main.winOnly"], cat="build", oracle="spec", note="call inside plat_windows.go (GOOS=windows build only)"),
    dict(id="BC11", token="platform", expect=["main.platform"], cat="build", oracle="spec", note="call from plat_windows.go into common plat.go"),
    dict(id="BC03", token="Hello", expect=[f"{L}.Hello"], cat="build", oracle="spec", note="inside //go:build integration file"),
    dict(id="BC03b", marker="BC03", token="localIntegOnly", expect=["main.localIntegOnly"], cat="build", oracle="spec", note="integration-only callee"),
    dict(id="BC04", token="Hello", expect=[f"{L}.Hello"], cat="build", oracle="spec", note="inside //go:build ignore file (standalone package main)"),
    dict(id="BC05", token="cadd", expect=["c:cadd"], cat="build", oracle="spec", note="cgo call C.cadd"),
    dict(id="BC06", token="Add", expect=["example.com/app/internal/cg.Add"], cat="build", oracle="trace", note="cgo build picks cg.go; CGO_ENABLED=0 picks cg_nocgo.go",
         expect_file="app/internal/cg/cg.go", alt=["app/internal/cg/cg_nocgo.go"]),
    dict(id="BC07", token="Add", expect=["asm:example.com/app/internal/asm.Add"], cat="build", oracle="spec", note="body-less decl, body in asm_amd64.s"),
    dict(id="BC08", token="runtimeNanotime", expect=["runtime.nanotime"], cat="build", oracle="spec", note="//go:linkname pull of runtime.nanotime"),
    dict(id="BC09", token="hiddenSecret", expect=["example.com/lib/hidden.secret"], cat="build", oracle="trace", note="//go:linkname pull of an in-repo function"),
    # --- generated code
    dict(id="GC01", token="Hello", expect=[f"{L}.Hello"], cat="generated", oracle="trace", note="call inside a Code generated file"),
    dict(id="GC02", token="generatedHelper", expect=["main.generatedHelper"], cat="generated", oracle="trace", note="call into a Code generated file"),
    # --- position encoding
    dict(id="EN01", token="héllo", expect=["main.héllo"], cat="encoding", oracle="trace", note="BMP non-ASCII before the token"),
    dict(id="EN02", token="𝔊", expect=["main.𝔊"], cat="encoding", oracle="trace", note="astral char before the token and as the token"),
    # --- tests
    dict(id="TS01", token="internalHelper", expect=[f"{T}.internalHelper"], cat="test", oracle="trace", note="in-package _test.go"),
    dict(id="TS02", token="Stamp", expect=[f"{T}.Stamp"], cat="test", oracle="trace"),
    dict(id="TS03", token="Tick", expect=[f"{T}.Tick"], cat="test", oracle="trace"),
    dict(id="TS04", token="InternalHelper", expect=[f"{T}.internalHelper"], cat="test", oracle="trace", note="external test pkg, func var from export_test.go"),
    dict(id="TS05", token="Now", expect=[f"{T}.realClock.Now"], cat="test", oracle="trace", note="non-test file; test-only implementer fakeClock",
         alt=[f"{T}.fakeClock.Now"]),
    dict(id="TS06", token="nowFn", expect=[f"{T}.defaultNow"], cat="test", oracle="trace", note="func var reassigned by tests", alt=[f"{T}.TestIn.func@20"]),
    dict(id="TS07", token="internalHelper", expect=[f"{T}.internalHelper"], cat="test", oracle="trace", note="call inside export_test.go"),
    dict(id="TS08", token="ExportedForTest", expect=[f"{T}.ExportedForTest"], cat="test", oracle="trace", note="external test calls func declared in export_test.go"),
    dict(id="TS09", token="Stamp", expect=[f"{T}.Stamp"], cat="test", oracle="trace"),
    dict(id="TS11", token="Hook", expect=[f"{T}.inner.Hook"], cat="test", oracle="trace", note="promoted inner.Hook in production; the test variant adds Outer.Hook (testy_test.go) which shadows it",
         alt=[f"{T}.Outer.Hook"]),
    dict(id="TS10", token="Tick", expect=[f"{T}.Tick"], cat="test", oracle="trace"),
]
