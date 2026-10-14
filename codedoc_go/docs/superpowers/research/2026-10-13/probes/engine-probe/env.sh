# Source this at the start of every shell (engine-probe track).
export R=/home/admin/scratch/codedoc-go-research W=/home/admin/scratch/codedoc-go-research/engine-probe
export GOROOT=$R/toolchain/go PATH=$R/toolchain/go/bin:$R/gopath/bin:$PATH
export GOPATH=$R/gopath GOMODCACHE=$R/gomodcache-shared GOCACHE=$R/gocache-shared
export GOENV=off GOTOOLCHAIN=local GOFLAGS=-modcacherw TMPDIR=$W/tmp GOTMPDIR=$W/tmp NO_COLOR=1
# Keep go/gopls telemetry counters and the gopls file cache under $W (they default to ~/.config/go, ~/.cache/gopls).
export XDG_CONFIG_HOME=$W/xdgconfig XDG_CACHE_HOME=$W/xdgcache
export GOPLS_SRC=$R/gomodcache-shared/golang.org/x/tools/gopls@v0.23.0
export XTOOLS_SRC=$R/gomodcache-shared/golang.org/x/tools@v0.47.1-0.20260707181000-a299dadba899
