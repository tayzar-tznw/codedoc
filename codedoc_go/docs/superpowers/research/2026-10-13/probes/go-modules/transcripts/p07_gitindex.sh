#!/bin/bash
# Which go list forms run git (VCS stamping), and does that rewrite .git/index? Local throwaway repo only.
. /home/admin/scratch/codedoc-go-research/go-modules/transcripts/env.sh
export GOPROXY=off GOWORK=off
P=$W/p07-safety/gitindex; rm -rf $P; mkdir -p $P/bin $P/repo
printf '#!/bin/sh\necho "git $*" >> %s/log\nexec /usr/bin/git "$@"\n' $P > $P/bin/git; chmod +x $P/bin/git
cd $P/repo && /usr/bin/git init -q . && printf 'module example.com/vcs\n\ngo 1.22\n' > go.mod && printf 'package main\n\nfunc main() {}\n' > main.go && /usr/bin/git add . && /usr/bin/git -c user.email=x@y -c user.name=x commit -qm i
for f in "-json" "-json -buildvcs=false" "-e -f {{.ImportPath}}" "-e -json=ImportPath,Stale" "-e -json=Dir,ImportPath,GoFiles,CgoFiles,TestGoFiles,XTestGoFiles,IgnoredGoFiles,InvalidGoFiles"; do
  touch main.go; sleep 1.1; before=$(stat -c '%y' .git/index); : > $P/log
  PATH=$P/bin:$PATH GOCACHE=$P/gc go list $f ./... >/dev/null 2>&1
  after=$(stat -c '%y' .git/index)
  echo "go list $f -> git calls=$(grep -c '^git ' $P/log) [$(grep -m1 '^git ' $P/log)] .git/index rewritten=$([ "$before" = "$after" ] && echo no || echo yes)"
done
for g in "GIT_OPTIONAL_LOCKS=0 /usr/bin/git status --porcelain" "/usr/bin/git --no-optional-locks status --porcelain" "/usr/bin/git status --porcelain"; do
  touch main.go; sleep 1.1; before=$(stat -c '%y' .git/index); eval "$g" >/dev/null; after=$(stat -c '%y' .git/index)
  echo "$g -> .git/index rewritten=$([ "$before" = "$after" ] && echo no || echo yes)"
done
/usr/bin/git --version
