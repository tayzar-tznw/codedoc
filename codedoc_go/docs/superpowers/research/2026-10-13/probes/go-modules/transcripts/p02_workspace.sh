#!/bin/bash
# Workspace probes: MVS, replace precedence/conflicts, use-order, duplicate module paths, go version checks, GOWORK outside repo.
. /home/admin/scratch/codedoc-go-research/go-modules/transcripts/env.sh
P=$W/p02-gowork/t; rm -rf $P; mkdir -p $P/ws/a $P/ws/b $P/ws/forkA $P/ws/forkB $P/ws/forkW $P/out/gen1 $P/out/gen2 $P/out/gen3; cd $P/ws
for x in A B W; do printf 'module example.com/dep\n\ngo 1.21\n' > fork$x/go.mod; printf 'package dep\n\nfunc Version() string { return "fork%s" }\n' $x > fork$x/dep.go; done
base_a='module example.com/a\n\ngo 1.22\n\nrequire example.com/dep v1.0.0\n'; base_b='module example.com/b\n\ngo 1.22\n\nrequire example.com/dep v1.2.0\n'
printf "$base_a" > a/go.mod; printf 'package a\n\nimport "example.com/dep"\n\nfunc A() string { return dep.Version() }\n' > a/a.go
printf "$base_b" > b/go.mod; printf 'package b\n\nimport "example.com/dep"\n\nfunc B() string { return dep.Version() }\n' > b/b.go
(cd a && GOWORK=off go mod tidy 2>/dev/null); (cd b && GOWORK=off go mod tidy 2>/dev/null)
ws() { printf "go 1.22\n\nuse (\n$(for u in "$@"; do printf '\t%s\\n' $u; done))\n" > go.work; }
q() { go list -f '{{.Module.Path}}@{{.Module.Version}} replace={{with .Module.Replace}}{{.Path}}{{.Version}}{{end}} dir={{.Dir}}' example.com/dep 2>&1 | sed "s#$P/##g; s#$W/##g"; }
ws ./a ./b
echo "T1 MVS  a GOWORK=off: $(cd a && GOWORK=off q)"; echo "T1 MVS  a workspace : $(cd a && q)"
printf "$base_a\nreplace example.com/dep => ../forkA\n" > a/go.mod
echo "T2 a:wildcard=>forkA b:none            from b: $(cd b && q)"
printf "$base_b\nreplace example.com/dep => ../forkB\n" > b/go.mod
echo "T3 a:=>forkA b:=>forkB (conflict)      from b: $(cd b && q | tr '\n' ' ')"
printf 'replace example.com/dep => ./forkW\n' >> go.work
echo "T4 + go.work wildcard=>forkW           from b: $(cd b && q)"
ws ./a ./b; printf "$base_b\nreplace example.com/dep => ./../forkA\n" > b/go.mod
echo "T6 a:../forkA b:./../forkA (same dir)  from b: $(cd b && q)"
printf "$base_a\nreplace example.com/dep v1.0.0 => ../forkA\n" > a/go.mod; printf "$base_b" > b/go.mod
echo "T5 a:v1.0.0=>forkA, MVS picks v1.2.0   ws: $(cd a && q) | GOWORK=off: $(cd a && GOWORK=off q)"
printf "$base_a\nreplace example.com/dep v1.2.0 => ../forkA\n" > a/go.mod; printf "$base_b\nreplace example.com/dep => ../forkB\n" > b/go.mod
ws ./a ./b; echo "T8 use(a,b) a:v1.2.0=>forkA b:wildcard=>forkB: $(cd a && q | head -1)"
ws ./b ./a; echo "T8r use(b,a) same replaces:                 $(cd a && q | head -1)"
printf "$base_b\nreplace example.com/dep v1.2.0 => ../forkA\nreplace example.com/dep => ../forkB\n" > b/go.mod
echo "T10 single go.mod with specific+wildcard (GOWORK=off): $(cd b && GOWORK=off q)"
printf "$base_a" > a/go.mod; printf "$base_b" > b/go.mod
mkdir -p a2 && printf 'module example.com/a\n\ngo 1.22\n' > a2/go.mod && printf 'package a\n' > a2/a.go
ws ./a ./a2 ./b; echo "T11 two use dirs, same module path: $(cd b && go list ./... 2>&1 | sed "s#$P/##g")"
mkdir -p nomod; ws ./a ./nomod; echo "T12 use dir without go.mod: $(cd a && go list ./... 2>&1 | sed "s#$P/##g")"
printf 'go 1.21\n\nuse (\n\t./a\n\t./b\n)\n' > go.work; echo "T14 go.work go 1.21 < module go 1.22: $(cd a && go list ./... 2>&1 | head -1 | sed "s#$P/##g")"
printf 'go 1.28\n\nuse (\n\t./a\n\t./b\n)\n' > go.work; echo "T15 go.work go 1.28 > toolchain: $(cd a && go list ./... 2>&1 | sed "s#$P/##g") | go work edit -json Go=$(cd a && go work edit -json | grep '"Go"')"
printf 'use (\n\t./a\n\t./b\n)\n' > go.work; echo "T16 go.work without go line: $(cd a && go list ./... 2>&1 | head -1 | sed "s#$P/##g")"
rm -rf a2 nomod; ws ./a ./b
cd $P/out; f='{{.ImportPath}} main={{.Module.Main}} dep={{.Dir}}'
printf "go 1.22\n\nuse (\n\t$P/ws/a\n\t$P/ws/b\n)\n" > gen1/go.work
echo "G1 GOWORK=/abs/out/gen1/go.work, absolute use, cwd=/: $(cd / && GOWORK=$P/out/gen1/go.work go list -f "$f" example.com/a example.com/dep 2>&1 | sed "s#$P/##g; s#$W/##g" | tr '\n' ' ')"
printf "go 1.22\n\nuse (\n\t../../ws/a\n\t../../ws/b\n)\n" > gen2/go.work
echo "G2 relative use paths resolve against the go.work dir: $(cd / && GOWORK=$P/out/gen2/go.work go list -f '{{.Dir}}' example.com/a 2>&1 | sed "s#$P/##g")"
echo "G3 GOWORK relative: $(cd $P/out && GOWORK=gen1/go.work go list example.com/a 2>&1)"
cp gen1/go.work gen1/work.txt; echo "G4 GOWORK=/abs/work.txt (no .work suffix): $(cd / && GOWORK=$P/out/gen1/work.txt go list example.com/a 2>&1)"
echo "G5 GOWORK missing file: $(cd / && GOWORK=$P/out/nonexist/go.work go list example.com/a 2>&1 | sed "s#$P/##g")"
printf "go 1.22\n\nuse $P/ws/a\n" > gen3/go.work
echo "G8 GOWORK overrides ws/go.work found from cwd: $(cd $P/ws/a && GOWORK=$P/out/gen3/go.work go list -f '{{.Module.Version}}' example.com/dep 2>&1)  (ws/go.work would give v1.2.0)"
echo "G9 pattern outside used dirs: $(cd / && GOWORK=$P/out/gen3/go.work go list $P/ws/b/... 2>&1 | sed "s#$P/##g; s#home/admin.*/t/##")"
echo "files written next to generated go.work files: $(ls gen1 gen2 gen3 | tr '\n' ' ')"
