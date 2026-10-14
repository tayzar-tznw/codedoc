#!/bin/bash
. /home/admin/scratch/codedoc-go-research/go-modules/transcripts/env.sh
echo "##### p03 vendor"
P=$W/p03-vendor/t; rm -rf $P; mkdir -p $P/v1/local && cd $P/v1
printf 'module example.com/v1\n\ngo 1.22\n\nrequire (\n\texample.com/dep v1.1.0\n\texample.com/dep/v2 v2.0.0\n\texample.com/Upper v1.0.0\n\texample.com/loc v0.0.0\n)\n\nreplace example.com/loc => ./local\n' > go.mod
printf 'module example.com/loc\n\ngo 1.22\n' > local/go.mod; printf 'package loc\n\nfunc L() {}\n' > local/l.go
printf 'package v1\n\nimport (\n\t"example.com/dep"\n\t"example.com/dep/sub"\n\tdep2 "example.com/dep/v2"\n\tupper "example.com/Upper"\n\t"example.com/loc"\n)\n\nfunc F() string { loc.L(); upper.U(); return dep.Version() + sub.Sub() + dep2.Version() }\n' > v1.go
GOWORK=off GOFLAGS=-mod=mod go mod tidy 2>/dev/null; GOWORK=off go mod vendor; echo "--- vendor/modules.txt"; cat vendor/modules.txt
echo "--- V1 default (vendor dir + go 1.22) with GOPROXY=off and an empty GOMODCACHE:"; GOWORK=off GOPROXY=off GOMODCACHE=$P/empty go list -f '{{.ImportPath}} mod={{with .Module}}{{.Path}}@{{.Version}}{{end}} dir={{.Dir}}' example.com/dep example.com/loc 2>&1 | sed "s#$P/##g"
echo "--- V1 go list -m all in vendor mode:"; GOWORK=off GOPROXY=off go list -m all 2>&1
cp go.mod go.mod.bak; sed -i 's/example.com\/dep v1.1.0/example.com\/dep v1.2.0/' go.mod
echo "--- V2 go.mod changed without re-vendoring:"; GOWORK=off GOPROXY=off go list . 2>&1 | sed "s#$P/##g"; cp go.mod.bak go.mod
sed -i 's/^go 1.22$/go 1.13/' go.mod; echo "--- V3 go 1.13 + vendor dir (vendor not used):"; GOWORK=off GOPROXY=off GOMODCACHE=$P/empty2 go list -e -f '{{.Dir}} {{.Error}}' example.com/dep 2>&1 | sed "s#$P/##g"; cp go.mod.bak go.mod
mv vendor/modules.txt $P/m.txt; echo "--- V4 vendor dir without modules.txt:"; GOWORK=off GOPROXY=off go list . 2>&1 | head -3 | sed "s#$P/##g"; mv $P/m.txt vendor/modules.txt
mkdir -p $P/wsv && cp -a $P/v1 $P/wsv/m && printf 'go 1.22\n\nuse ./m\n' > $P/wsv/go.work
echo "--- V5 same module inside a workspace (module vendor dir ignored):"; (cd $P/wsv/m && GOPROXY=off GOMODCACHE=$P/empty3 go list -e -f '{{.Dir}} {{.Error}}' example.com/dep 2>&1 | sed "s#$P/##g")
echo "--- V5b explicit -mod=vendor in workspace mode:"; (cd $P/wsv/m && GOPROXY=off GOMODCACHE=$P/empty3 go list -mod=vendor . 2>&1 | head -2 | sed "s#$P/##g")
echo "--- V6 -mod=mod in workspace mode:"; (cd $P/wsv/m && GOFLAGS=-mod=mod go list . 2>&1 | head -1)
rm -rf $P/empty*
echo; echo "##### p04 packages (see also p04-packages/m)"
cd $W/p04-packages/m && GOWORK=off GOPROXY=off go list -e -f '{{.ImportPath}} go={{.GoFiles}} test={{.TestGoFiles}} xtest={{.XTestGoFiles}} ignored={{.IgnoredGoFiles}} err={{if .Error}}{{.Error.Err}}{{end}}' ./... 2>&1 | sed "s#$W/##g"
echo "--- tree:"; find . -name '*.go' | sort | tr '\n' ' '; echo; echo "--- go.mod:"; cat go.mod
cd $W/p04-packages/vend && echo "--- vendor-named package: $(GOWORK=off GOPROXY=off go list ./... | tr '\n' ' ') (tree: $(find . -name '*.go' | tr '\n' ' '))"
echo; echo "##### p06 toolchain"
P=$W/p06-toolchain/t; rm -rf $P; mkdir -p $P/m && cd $P/m && printf 'module example.com/tc\n\ngo 1.28\n\ntoolchain go1.28.1\n' > go.mod && printf 'package tc\n\nfunc F() int { return G() }\n\nfunc G() int { return 1 }\n' > tc.go
echo "go mod edit -json (local): $(GOWORK=off GOPROXY=off go mod edit -json | tr -d '\n\t')"
echo "go list (local): $(GOWORK=off GOPROXY=off go list ./... 2>&1)"
echo "go list (auto, GOPROXY=off): $(GOWORK=off GOTOOLCHAIN=auto GOPROXY=off go list ./... 2>&1 | tr '\n' ' ')"
echo "go env GOMOD (local): $(GOWORK=off go env GOMOD | sed "s#$P/##")"
printf 'module example.com/tc\n\ngo 1.27.9\n' > go.mod; echo "go 1.27.9 (newer patch): $(GOWORK=off GOPROXY=off go list ./... 2>&1)"
printf 'module example.com/tc\n\ngo 1.26\n\ntoolchain go1.28.1\n' > go.mod; echo "toolchain line newer, go line older: $(GOWORK=off GOPROXY=off go list ./... 2>&1)"
printf 'module example.com/tc\n\ngo 1.28\n' > go.mod
echo "gopls definition (GOTOOLCHAIN=local, go 1.28): $(cd $P/m && GOWORK=off GOPROXY=off GOPLSCACHE=$P/gc timeout 60 $R/gopath/bin/gopls definition tc.go:3:23 2>&1 | grep -m1 -o 'go: go.mod requires.*')"
echo "gopls definition (GOTOOLCHAIN=auto, go 1.28): $(cd $P/m && GOWORK=off GOPROXY=off GOTOOLCHAIN=auto GOPLSCACHE=$P/gc timeout 60 $R/gopath/bin/gopls definition tc.go:3:23 2>&1 | grep -m1 -o 'go: downloading go1.28.*')"
mkdir -p $P/dep && cd $P/dep && printf 'module example.com/usesnew\n\ngo 1.22\n\nrequire example.com/newgo v1.0.0\n' > go.mod && printf 'package usesnew\n\nimport "example.com/newgo"\n\nfunc F() { newgo.N() }\n' > u.go
echo "dependency with go 1.28: $(GOWORK=off go mod download example.com/newgo 2>&1)"
