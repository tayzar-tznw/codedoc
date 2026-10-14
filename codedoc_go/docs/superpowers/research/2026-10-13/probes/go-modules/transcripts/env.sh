export R=/home/admin/scratch/codedoc-go-research W=/home/admin/scratch/codedoc-go-research/go-modules
export GOROOT=$R/toolchain/go PATH=$R/toolchain/go/bin:/usr/bin:/bin
export GOPATH=$W/gopath-probe GOMODCACHE=$W/modcache-probe GOCACHE=$W/gocache-probe
export GOENV=off GOTOOLCHAIN=local GOFLAGS=-modcacherw TMPDIR=$W/tmp GOTMPDIR=$W/tmp NO_COLOR=1
export GOPROXY=file://$W/proxy GOSUMDB=off XDG_CONFIG_HOME=$W/p07-safety/xdg2
unset GOWORK
