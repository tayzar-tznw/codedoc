#!/bin/bash
# Timing matrix for approach B (helper); every MEASURE line is raw evidence. Writes under $W/out/timing-*.
source /home/admin/scratch/codedoc-go-research/real-repos/env.sh
export GOPROXY=off
for i in 1 2; do
  for r in etcd prometheus; do
    python3 $W/bin/measure.py $r-B-download-warm-$i -- $W/helper/helper -root $W/repos/$r -out $W/out/timing-$r-dl-$i 2>&1 | grep -E 'MEASURE|loaded'
    GOMODCACHE=$W/gomodcache-empty python3 $W/bin/measure.py $r-B-offline-$i -- $W/helper/helper -root $W/repos/$r -out $W/out/timing-$r-off-$i 2>&1 | grep -E 'MEASURE|loaded'
    rm -rf $W/caches/gocache-timing-cold
    GOCACHE=$W/caches/gocache-timing-cold python3 $W/bin/measure.py $r-B-download-coldgocache-$i -- $W/helper/helper -root $W/repos/$r -out $W/out/timing-$r-cold-$i 2>&1 | grep -E 'MEASURE|loaded'
  done
done
