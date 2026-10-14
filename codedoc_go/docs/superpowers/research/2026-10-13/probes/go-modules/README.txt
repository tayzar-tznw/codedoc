go-modules research track (2026-10-13). Probe index:
docs/            fetched go.dev pages (ref/mod, doc/toolchain, go1.21..go1.27 notes) as html+txt; go1.27.2 `go help` texts
scripts/         html2txt.py, mkproxy.py (file:// GOPROXY), gobuildctx.py (build-constraint model), validate_constraints.py,
                 stdlist.py, gomodparse.py (go.mod/go.work parser), validate_gomod.py, listsummary.py
proxy/           synthetic file:// module proxy (example.com/dep v1.0.0..v1.2.0, /v2, !upper, newgo (go 1.28), nested)
p01-modfile/     go mod edit -json probes; lex.out (31 lexical cases); corpus/result.txt (3,682 go.mod parser comparison); work/
p02-gowork/      workspace MVS / replace precedence / GOWORK outside repo (see report §2 transcripts)
p03-vendor/      vendor defaults, modules.txt, consistency errors, go work vendor
p04-packages/    ./... directory and file rules, vendor-named package
p05-constraints/ run.out (603 rows, 167,184 files, 0 mismatches), run-promws.out, golist/*.json
p06-toolchain/   go 1.28 under GOTOOLCHAIN=local/auto, gopls inheritance, dependency requiring newer go
p07-safety/      cgo/pkg-config spies, telemetry, GOPRIVATE->git, GOFLAGS/toolexec/GOCACHEPROG, buildvcs git calls, go.sum writes
p08-offline/     etcd/prometheus offline loads (O*, P*, N*, R*), file:// proxy over a read-only cache, nested-module ambiguity (amb/)
p09-stdlib/      std package list: python vs go list std (7 targets)
p10-gopath/      GO111MODULE=off probe
p11-identity/    go list -m variants offline
p12-download/    run.sh/all.sh, runs/*/summary.txt (size/inodes/time per variant), verify/ (gopls-flag offline loads), spy/ (hosts, VCS)
incident/        go.work.sum accidentally created in the shared Prometheus clone at 19:56:27, moved here at 19:57
Added after the first report:
transcripts/p07_gitindex.*  which go list forms run git; .git/index rewrites; GIT_OPTIONAL_LOCKS=0
p12-download/spy-moddl/     go mod download (etcd api, GOWORK=off) under VCS spies: 50 GETs to proxy.golang.org, 0 VCS calls
p03-vendor/wv-gen/          generated go.work + symlinked workspace vendor dir: vendor mode used
p11-identity/I3I4.txt       replaced in-repo module seen in single-module vs workspace mode
p08-offline/amb/transcript.txt  nested-module ambiguity: parent zip required offline
