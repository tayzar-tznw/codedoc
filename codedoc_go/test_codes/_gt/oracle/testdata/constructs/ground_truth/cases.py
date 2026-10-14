import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
from gt_lib import case, external, plain, resolved, target  # noqa: E402

LIB = "lib/lib.go"
MAIN = "cmd/run/main.go"
M = "example.com/constructs"
RUN = "run"


def t(qualname, file=LIB, kind="method"):
    return target(qualname, M, kind, file)


# WANT maps every case to the verdict goracle judge must give (tests only).
WANT = {}


def want(verdict, c):
    WANT[c["id"]] = verdict
    return c


CASES = [
    want("CONFIRMED", case("CALL-001", LIB, "total += Double(v)", "Double", "call", resolved(t("Double")),
                           "static call in a range-over-func body", entry=RUN)),
    want("CONFIRMED-NOEXEC", case("CALL-002", LIB, 'return Note("A")', "Note", "call", resolved(t("Note")),
                                  "call in a return statement", entry=RUN)),
    want("CONFIRMED-NOEXEC", case("CALL-003", LIB, "a && Check(b)", "Check", "call", resolved(t("Check")),
                                  "right operand of &&", entry=RUN)),
    want("CONFIRMED", case("GEN-001", MAIN, "lib.Max(2, 3)", "Max", "call", resolved(t("Max")), "generic func",
                           entry=RUN)),
    want("CONFIRMED", case("GEN-002", MAIN, "(&lib.Box[int]{}).Get()", "Get", "call", resolved(t("Box.Get")),
                           "method of a generic type", entry=RUN)),
    want("CONFIRMED", case("MEX-001", MAIN, "(*lib.Cat).Sound(&lib.Cat{})", "Sound", "call",
                           resolved(t("Cat.Sound")), "method expression", entry=RUN)),
    want("IFACE-CONSISTENT", case("IFC-001", LIB, "out += a.Sound()", "Sound", "call",
                                  resolved(t("Animal.Sound"), runtime=[t("Dog.Sound"), t("Cat.Sound")],
                                           candidates=[t("Dog.Sound"), t("Cat.Sound")], open_world=False),
                                  "interface method with two implementers", entry=RUN)),
    want("IFACE-OTHER", case("IFC-002", MAIN, "lib.Chorus(lib.Dog{}, &lib.Cat{})", "Chorus", "call",
                             resolved(t("Animal.Sound"), runtime=[t("Dog.Sound")]),
                             "deliberately wrong: Chorus is a plain function", entry=RUN)),
    want("CONSISTENT-NOEXEC", case("FV-001", LIB, "return o.F(x)", "F", "call", plain("dynamic", runtime=[t("Double")]),
                            "func field", entry=RUN)),
    want("CONSISTENT-NOEXEC", case("FV-002", LIB, 'm["inc"](x)', "m", "call",
                            plain("dynamic", runtime=[t("main.func1", MAIN)]), "map of funcs", entry=RUN)),
    want("MISS", case("FV-003", MAIN, "fmt.Println(sound(),", "sound", "call", plain("dynamic"),
                      "deliberately wrong: the method value's target is undocumented", entry=RUN)),
    want("CONSISTENT", case("FV-004", MAIN, 'lib.Shout("hi")', "Shout", "call",
                            plain("dynamic", runtime=[t("<init>.func1")]), "package-level func variable",
                            entry=RUN)),
    want("CONFIRMED-NOEXEC", case("RNG-001", LIB, "range c.All", "All", "range", resolved(t("Counter.All")),
                                  "range over a method value", entry=RUN)),
    want("CONSISTENT-NOEXEC", case("RNG-002", LIB, "for v := range seq", "seq", "range",
                            plain("dynamic", runtime=[t("Seq.func1")]), "range over a func-typed local",
                            entry=RUN)),
    want("CONFIRMED-NOEXEC", case("GO-001", LIB, "go send(done, 3)", "send", "go", resolved(t("send")),
                                  "go statement", entry=RUN)),
    want("CONFIRMED-NOEXEC", case("GO-002", LIB, "go func() { done <- Double(1) }()", "func", "go",
                                  resolved(t("Spawn.func1")), "go statement with a func literal", entry=RUN)),
    want("UNVERIFIABLE", case("DEF-001", LIB, "defer func() {", "func", "defer", resolved(t("Cleanup.func1")),
                              "deferred closure that runs while the function panics", entry=RUN)),
    want("NOT-RUN", case("DEF-002", LIB, 'defer record(log, "extra")', "record", "defer", resolved(t("record")),
                         "conditional defer never taken, yet the case names an entry", entry=RUN)),
    want("CONSISTENT-NOEXEC", case("EXT-001", LIB, "return strings.ToUpper(s)", "ToUpper", "call", external("std"),
                            "stdlib call in a return", entry=RUN)),
    want("CONSISTENT-NOEXEC", case("EXT-002", LIB, "switch strings.ToLower(k)", "ToLower", "call", external("std"),
                            "stdlib call in a switch tag (reach counter)", entry=RUN)),
    want("CONSISTENT", case("BLT-001", LIB, 'order = append(order, "first")', "append", "call", plain("builtin"),
                            "builtin", entry=RUN)),
    want("MISS", case("WRG-001", MAIN, "lib.SumSeq(3)", "SumSeq", "call", resolved(t("Double")),
                      "deliberately wrong target", entry=RUN, distractors=[t("SumSeq")])),
    want("MISS", case("WRG-002", LIB, "n, _ := w.Write", "Write", "call", resolved(t("upper.Write")),
                      "deliberately wrong: bytes.Buffer.Write also runs here", entry=RUN)),
    want("UNEXEC", case("BLD-001", "lib/excluded.go", "return Double(21)", "Double", "call", resolved(t("Double")),
                        "file excluded by //go:build ignore", entry=None)),
]
PAN = "lib/panics.go"
CASES += [
    want("CONFIRMED-NOEXEC", case("PAN-001", PAN, "panic(compute())", "compute", "call", resolved(t("compute", PAN)),
                                  "the deferred call that runs after the recover is not this site's", entry=RUN)),
    want("DEFER-GROUP-CONSISTENT", case("PAN-002", PAN, "defer cleanup()", "cleanup", "defer",
                                        resolved(t("cleanup", PAN)),
                                        "runtime.deferreturn runs it after the recover", entry=RUN)),
    want("CONFIRMED-NOEXEC", case("PAN-003", PAN, "_ = Crash()", "Crash", "call", resolved(t("Crash", PAN)),
                                  "the call panics: entries outnumber completions", entry=RUN)),
    want("CONFIRMED-NOEXEC", case("PAN-004", PAN, "defer cleanupPtr()", "cleanupPtr", "defer",
                                  resolved(t("cleanupPtr", PAN)),
                                  "the call through *FP on the return line is tagged, not part of the group",
                                  entry=RUN)),
    want("CONFIRMED-NOEXEC", case("VAR-001", "lib/vars.go", "compute()", "compute", "call",
                                  resolved(t("compute", PAN)), "package-level initializer in a file without functions",
                                  entry=RUN)),
]
DF = "lib/defers.go"
CASES += [
    want("CONFIRMED-NOEXEC", case("PAN-005", DF, "panic(compute2())", "compute2", "call", resolved(t("compute2", DF)),
                                  "the wrapped deferred call (deferwrap) that runs after the recover is not this site's",
                                  entry=RUN)),
    want("DEFER-GROUP-CONSISTENT", case("PAN-006", DF, "defer cleanupArg(1)", "cleanupArg", "defer",
                                        resolved(t("cleanupArg", DF)),
                                        "deferreturn runs the deferwrap closure, which runs cleanupArg", entry=RUN)),
    want("CONFIRMED-NOEXEC", case("PAN-007", DF, "panic(compute3())", "compute3", "call", resolved(t("compute3", DF)),
                                  "a deferred method call is wrapped too", entry=RUN)),
    want("DEFER-GROUP-CONSISTENT", case("PAN-008", DF, "defer w.Close()", "Close", "defer",
                                        resolved(t("closer.Close", DF)), "deferred method value after a recover",
                                        entry=RUN)),
    want("DEFER-GROUP-CONSISTENT", case("RNG-003", DF, "defer cleanupR2()", "cleanupR2", "defer",
                                        resolved(t("cleanupR2", DF)),
                                        "a return inside a range-over-func body exits at the loop's closing brace",
                                        entry=RUN)),
    want("DEFER-GROUP-CONSISTENT", case("RNG-004", DF, "defer cleanupR()", "cleanupR", "defer",
                                        resolved(t("cleanupR", DF)), "runs on both exits", entry=RUN)),
    want("DEFER-GROUP-OTHER", case("GIF-001", DF, "defer c.Close()", "Close", "defer",
                                   resolved(t("Closer2.Close", DF), runtime=[t("ca.Close", DF), t("cb.Close", DF)],
                                            open_world=False),
                                   "deliberately wrong: cb.Close never runs here", entry=RUN)),
    want("DEFER-GROUP-CONSISTENT", case("GIF-002", DF, "defer d.Close()", "Close", "defer",
                                        resolved(t("Closer2.Close", DF), runtime=[t("cb.Close", DF)],
                                                 open_world=False),
                                        "interface method deferred next to another defer", entry=RUN)),
    want("UNVERIFIABLE", case("OWF-001", DF, 's.WriteString("x")', "WriteString", "call",
                              resolved(t("Sink.WriteString", DF), open_world=True),
                              "an else-if condition has no counter, and only strings.Builder implements here",
                              entry=RUN)),
]
DECL_PINS = [{"qualname": "Counter", "module": M, "kind": "class", "file": LIB}]
