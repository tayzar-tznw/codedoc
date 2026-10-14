#!/bin/bash
. /home/admin/scratch/codedoc-go-research/go-modules/transcripts/env.sh
export GOPROXY=off GOWORK=off
P=$W/p07-safety/t; rm -rf $P; mkdir -p $P/spy $P/m/c $P/m/plain; S=$P/spy
for t in cc pkg-config toolexec git; do printf '#!/bin/sh\necho "%s $*" >> %s/log\n' $t $S > $S/$t; done
echo 'exec gcc "$@"' >> $S/cc; echo 'exit 1' >> $S/pkg-config; echo 'exec "$@"' >> $S/toolexec; echo 'exit 128' >> $S/git; chmod +x $S/*
printf 'module example.com/m\n\ngo 1.22\n' > $P/m/go.mod
printf 'package c\n\n// #cgo pkg-config: nosuchlib\n// int add(int a, int b) { return a+b; }\nimport "C"\n\nfunc Add() int { return int(C.add(1, 2)) }\n' > $P/m/c/c.go
printf 'package plain\n\nimport "net"\n\nfunc F() { _ = net.IPv4len }\n' > $P/m/plain/p.go
cd $P/m; GL="go list -e -json=ImportPath,Error -test=true -export=false -deps=true -find=false -buildvcs=false -pgo=off"
n() { c=$(grep -c "^$1 " $S/log 2>/dev/null); echo ${c:-0}; }
echo "##### cgo: what go list runs (fresh GOCACHE each)"
: > $S/log; GOCACHE=$P/gc1 CC=$S/cc PKG_CONFIG=$S/pkg-config $GL -compiled=false -- ./... >/dev/null 2>&1; echo "S1 -compiled=false CGO_ENABLED=1: CC=$(n cc) pkg-config=$(n pkg-config)"
: > $S/log; GOCACHE=$P/gc2 CC=$S/cc PKG_CONFIG=$S/pkg-config $GL -compiled=true -- ./... >/dev/null 2>&1; echo "S2 -compiled=true  CGO_ENABLED=1: CC=$(n cc) pkg-config=$(n pkg-config) [$(grep -m1 pkg-config $S/log)]"
: > $S/log; GOCACHE=$P/gc2 CC=$S/cc PKG_CONFIG=$S/pkg-config $GL -compiled=true -- ./... >/dev/null 2>&1; echo "S2b warm GOCACHE: CC=$(n cc) pkg-config=$(n pkg-config)"
: > $S/log; CGO_ENABLED=0 GOCACHE=$P/gc3 CC=$S/cc PKG_CONFIG=$S/pkg-config $GL -compiled=true -- ./... >/dev/null 2>&1; echo "S3 -compiled=true  CGO_ENABLED=0: CC=$(n cc) pkg-config=$(n pkg-config); ./c under CGO_ENABLED=0: $(CGO_ENABLED=0 GOCACHE=$P/gc3 go list -e -f '{{.Error}} ignored={{.IgnoredGoFiles}}' ./c 2>&1 | sed "s#$P/##")"
echo "context: $(cd $W/tmp && go list -f 'CgoEnabled={{context.CgoEnabled}} ToolTags={{context.ToolTags}}' unsafe) | no gcc on PATH: $(cd $W/tmp && PATH=$R/toolchain/go/bin go list -f 'CgoEnabled={{context.CgoEnabled}}' unsafe) | CC=/nonexistent: $(cd $W/tmp && CC=/nonexistent go list -f 'CgoEnabled={{context.CgoEnabled}}' unsafe) | GOOS=windows: $(cd $W/tmp && GOOS=windows go list -f 'CgoEnabled={{context.CgoEnabled}}' unsafe)"
echo "##### other programs the go command executes"
: > $S/log; GOCACHE=$P/gc4 GOFLAGS=-toolexec=$S/toolexec $GL -compiled=true -- ./... >/dev/null 2>&1; echo "GOFLAGS=-toolexec, CGO_ENABLED=1: toolexec calls=$(n toolexec)"
: > $S/log; CGO_ENABLED=0 GOCACHE=$P/gc5 GOFLAGS=-toolexec=$S/toolexec $GL -compiled=true -- ./... >/dev/null 2>&1; echo "GOFLAGS=-toolexec, CGO_ENABLED=0: toolexec calls=$(n toolexec)"
: > $S/log; GOCACHE=$P/gc6 GOCACHEPROG="$S/toolexec /bin/false" go list -e -deps ./plain >/dev/null 2>&1; echo "GOCACHEPROG set, plain go list -deps: calls=$(n toolexec)"
mkdir -p $P/vcsrepo && cd $P/vcsrepo && /usr/bin/git init -q . && printf 'module example.com/vcs\n\ngo 1.22\n' > go.mod && printf 'package main\n\nfunc main() {}\n' > main.go && /usr/bin/git add . && /usr/bin/git -c user.email=x@y -c user.name=x commit -qm i
printf '#!/bin/sh\necho "git $*" >> %s/log\nexec /usr/bin/git "$@"\n' $S > $S/git2; chmod +x $S/git2; mkdir -p $S/gitbin && ln -sf $S/git2 $S/gitbin/git
for f in "-json" "-json -buildvcs=false"; do : > $S/log; PATH=$S/gitbin:$PATH GOCACHE=$P/gc7 go list $f ./... >/dev/null 2>&1; echo "go list $f in a git repo with a main package: git calls=$(n git)"; done
echo "##### GOPRIVATE/GONOPROXY vs GOPROXY=off (spy git on PATH)"
mkdir -p $P/vm && cd $P/vm && printf 'module example.com/vm\n\ngo 1.22\n\nrequire github.com/codedoc-nonexistent-org/private v1.0.0\n' > go.mod && printf 'github.com/codedoc-nonexistent-org/private v1.0.0 h1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\ngithub.com/codedoc-nonexistent-org/private v1.0.0/go.mod h1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n' > go.sum && printf 'package vm\n\nimport "github.com/codedoc-nonexistent-org/private"\n\nvar _ = private.X\n' > vm.go
mkdir -p $S/gitspy && cp $S/git $S/gitspy/git
for v in "GOPRIVATE=github.com/codedoc-nonexistent-org" "GOPRIVATE=github.com/codedoc-nonexistent-org GOVCS=*:off" "GOPRIVATE=" "GOPROXY=direct"; do : > $S/log; out=$(env PATH=$S/gitspy:$PATH GOMODCACHE=$P/mcv GOCACHE=$P/gc8 $v go list -e -f '{{.ImportPath}} {{.Error}}' -deps . 2>&1 | grep codedoc | grep -v '^go: downloading' | cut -c1-140); echo "GOPROXY=off(default here) $v: git calls=$(n git) | $out"; done
echo "##### go.sum / go.work.sum writes"
mkdir -p $P/sum/m $P/sum/out && cd $P/sum/m && printf 'module example.com/summ\n\ngo 1.22\n\nrequire example.com/dep v1.0.0\n' > go.mod && : > go.sum && printf 'package summ\n\nimport "example.com/dep"\n\nvar _ = dep.Version\n' > s.go
echo "single-module readonly, empty go.sum, module cached: $(GOSUMDB=off go list -e -f '{{.ImportPath}} {{.Error}}' -deps . 2>&1 | grep -m1 'example.com/dep') ; go.sum bytes=$(wc -c < go.sum)"
echo "GOFLAGS=-mod=mod inherited: $(GOFLAGS=-mod=mod go list ./... 2>&1); go.sum bytes after=$(wc -c < go.sum)"; : > go.sum
printf "go 1.22\n\nuse $P/sum/m\n" > $P/sum/out/go.work
echo "generated go.work outside the module, GOSUMDB=off: $(GOWORK=$P/sum/out/go.work go list -e -f '{{.ImportPath}} err={{.Error}}' -deps . 2>&1 | grep example.com/dep); module go.sum bytes=$(wc -c < go.sum); out/go.work.sum lines=$(wc -l < $P/sum/out/go.work.sum)"
echo "##### replace pointing outside the repo"
mkdir -p $P/out1/repo/a $P/out1/elsewhere/dep $P/out1/gen && cd $P/out1
printf 'module example.com/dep\n\ngo 1.21\n' > elsewhere/dep/go.mod; printf 'package dep\n\nfunc Version() string { return "outside" }\n' > elsewhere/dep/dep.go
printf "module example.com/a\n\ngo 1.22\n\nrequire example.com/dep v1.1.0\n\nreplace example.com/dep => $P/out1/elsewhere/dep\n" > repo/a/go.mod; printf 'package a\n\nimport "example.com/dep"\n\nfunc A() string { return dep.Version() }\n' > repo/a/a.go
qq() { go list -f '{{.Module.Path}}@{{.Module.Version}} replace={{with .Module.Replace}}{{.Path}} {{.Version}}{{end}} dir={{.Dir}}' example.com/dep 2>&1 | sed "s#$P/##g; s#$W/##g"; }
echo "O1 GOWORK=off honours absolute out-of-repo replace: $(cd repo/a && GOPROXY=file://$W/proxy qq)"
printf "go 1.22\n\nuse $P/out1/repo/a\n\nreplace example.com/dep => example.com/dep v1.1.0\n" > gen/go.work
echo "O2 generated go.work 'replace X => X v1.1.0' overrides it: $(cd gen && GOPROXY=file://$W/proxy GOWORK=$P/out1/gen/go.work qq)"
mkdir -p gen/stub && printf 'module example.com/dep\n\ngo 1.21\n' > gen/stub/go.mod && printf "go 1.22\n\nuse $P/out1/repo/a\n\nreplace example.com/dep => ./stub\n" > gen/go.work
echo "O3 generated go.work replace to an empty stub (fail closed): $(cd gen && GOWORK=$P/out1/gen/go.work go list -e -f '{{.ImportPath}} err={{.Error}}' -deps example.com/a 2>&1 | grep -m1 example.com/dep | cut -c1-120 | sed "s#$P/##g")"
echo "##### telemetry"
X=$P/xdg; mkdir -p $X; echo "GOTELEMETRY env ignored: GOTELEMETRY=off go env GOTELEMETRY -> $(GOTELEMETRY=off XDG_CONFIG_HOME= go env GOTELEMETRY)"
XDG_CONFIG_HOME=$X go list ./... >/dev/null 2>&1 ; echo "isolated XDG_CONFIG_HOME, mode unset: files written=$(find $X -type f | wc -l) ($(find $X -type f -name '*.count' | head -1 | sed "s#$X/##"))"
rm -rf $X; mkdir -p $X; XDG_CONFIG_HOME=$X go telemetry off; XDG_CONFIG_HOME=$X go list ./... >/dev/null 2>&1; echo "after 'go telemetry off' inside isolated XDG_CONFIG_HOME: files=$(find $X -type f | sed "s#$X/##" | tr '\n' ' ') content='$(cat $X/go/telemetry/mode)'"
