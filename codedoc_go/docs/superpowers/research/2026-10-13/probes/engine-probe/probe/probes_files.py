L = "example.com/lib"
SITES = [
    dict(id="FL01", token="Hello", expect=[f"{L}.Hello"], cat="files", oracle="spec", note="file under testdata/ (ignored by go list ./...)"),
    dict(id="FL02", token="Hello", expect=[f"{L}.Hello"], cat="files", oracle="spec", note="file under _hidden/ (ignored by the go command)"),
    dict(id="FL03", token="Hello", expect=[f"{L}.Hello"], cat="files", oracle="spec", note="file under .dotdir/ (ignored by the go command)"),
    dict(id="BC03", token="Hello", expect=[f"{L}.Hello"], cat="build", oracle="spec", note="inside //go:build integration file"),
    dict(id="BC04", token="Hello", expect=[f"{L}.Hello"], cat="build", oracle="spec", note="inside //go:build ignore file (standalone package main)"),
    dict(id="BC05", token="cadd", expect=["c:cadd"], cat="build", oracle="spec", note="cgo call C.cadd"),
    dict(id="BC06", token="Add", expect=["example.com/app/internal/cg.Add"], cat="build", oracle="trace", note="cgo build picks cg.go; CGO_ENABLED=0 picks cg_nocgo.go"),
    dict(id="TS05", token="Now", expect=[f"{L}/testy.realClock.Now"], cat="test", oracle="trace", note="non-test file in lib/testy"),
    dict(id="PK02", token="Hello", expect=[f"{L}.Hello"], cat="pkg", oracle="trace", note="qualified call"),
]
