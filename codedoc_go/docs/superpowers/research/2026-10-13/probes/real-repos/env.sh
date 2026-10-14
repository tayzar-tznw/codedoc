export R=/home/admin/scratch/codedoc-go-research W=/home/admin/scratch/codedoc-go-research/real-repos
export GOROOT=$R/toolchain/go PATH=$R/toolchain/go/bin:$R/gopath/bin:$PATH
export GOPATH=$R/gopath GOMODCACHE=$R/gomodcache-shared GOCACHE=$R/gocache-shared
export GOENV=off GOTOOLCHAIN=local GOFLAGS=-modcacherw TMPDIR=$W/tmp GOTMPDIR=$W/tmp NO_COLOR=1
