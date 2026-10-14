#!/bin/bash
# measure.sh TAG BASE INST HITS NAME=PKGDIR...: builds each test binary three
# ways (base, base with -l, instrumented with -l) and runs base and
# instrumented twice each. Writes $W/measure/TAG.tsv.
. /home/admin/scratch/codedoc-go-research/runtime-oracle/scripts/env.sh
TAG=$1 BASE=$2 INST=$3 HITS=$4; shift 4
OUT=$W/measure/$TAG; mkdir -p $OUT/bin
TSV=$W/measure/$TAG.tsv; printf 'pkg\tvariant\tsize_bytes\tbuild_s\trun1_s\trun2_s\tresult1\tresult2\n' > $TSV
rm -rf "$HITS"; mkdir -p "$HITS"
for spec in "$@"; do
  name=${spec%%=*} dir=${spec#*=}
  for variant in base base-l inst; do
    case $variant in
      base)   src=$BASE flags="" ;;
      base-l) src=$BASE flags="-gcflags=all=-l" ;;
      inst)   src=$INST flags="-gcflags=all=-l -ldflags=-checklinkname=0" ;;
    esac
    bin=$OUT/bin/$name.$variant.test
    t0=$(date +%s.%N)
    (cd $src && go test -c -vet=off $flags -o $bin ./$dir) > $OUT/$name.$variant.build.log 2>&1 || { echo "BUILD FAIL $name $variant"; continue; }
    t1=$(date +%s.%N)
    size=$(stat -c %s $bin)
    runs=() results=()
    if [ $variant != base-l ]; then
      for r in 1 2; do
        rs=$(date +%s.%N)
        if [ $variant = inst ]; then
          (cd $src/$dir && GORACLE_OUT=$HITS $bin -test.count=1 -test.timeout=40m) > $OUT/$name.$variant.run$r.log 2>&1
        else
          (cd $src/$dir && $bin -test.count=1 -test.timeout=40m) > $OUT/$name.$variant.run$r.log 2>&1
        fi
        rc=$?
        re=$(date +%s.%N)
        runs+=($(awk -v a=$re -v b=$rs "BEGIN{printf \"%.2f\", a-b}")) results+=($( [ $rc = 0 ] && echo PASS || echo FAIL$rc))
      done
    fi
    printf '%s\t%s\t%s\t%.1f\t%s\t%s\t%s\t%s\n' $name $variant $size $(awk -v a=$t1 -v b=$t0 "BEGIN{printf \"%.1f\", a-b}") "${runs[0]:--}" "${runs[1]:--}" "${results[0]:--}" "${results[1]:--}" | tee -a $TSV
  done
done
