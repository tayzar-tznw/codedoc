p01-modfile: go mod edit -json probes (Go 1.27.2, GOTOOLCHAIN=local, GOPROXY=off, GOWORK=off)
all/go.mod + all.json : every directive; sha256 unchanged after run
future/               : go 1.28 / toolchain go1.28.1 under GOTOOLCHAIN=local
lex/*.mod             : lexical edge cases (see transcript in report)
