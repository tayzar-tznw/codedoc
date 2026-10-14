#!/usr/bin/env python3
"""Run a command; report wall time, max child RSS (getrusage) and peak summed RSS of the process tree (sampled every 0.1 s).

usage: measure.py LABEL -- cmd args...
Prints one line 'MEASURE label=... wall_s=... maxrss_child_mb=... peak_tree_rss_mb=... rc=...' to stderr.
"""
import os, resource, subprocess, sys, time, threading

def tree_rss_kb(root):
    # sum VmRSS over root and all descendants
    children = {}
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/stat') as f:
                parts = f.read().rsplit(')', 1)[1].split()
            ppid = int(parts[1])
            children.setdefault(ppid, []).append(int(pid))
        except Exception:
            pass
    total, stack = 0, [root]
    while stack:
        p = stack.pop()
        try:
            with open(f'/proc/{p}/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        total += int(line.split()[1])
        except Exception:
            pass
        stack.extend(children.get(p, []))
    return total

def main():
    label = sys.argv[1]
    assert sys.argv[2] == '--'
    cmd = sys.argv[3:]
    t0 = time.monotonic()
    p = subprocess.Popen(cmd)
    peak = [0]
    stop = threading.Event()
    def sampler():
        while not stop.is_set():
            peak[0] = max(peak[0], tree_rss_kb(p.pid))
            stop.wait(0.1)
    th = threading.Thread(target=sampler, daemon=True); th.start()
    rc = p.wait()
    wall = time.monotonic() - t0
    stop.set(); th.join()
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(f'MEASURE label={label} wall_s={wall:.2f} maxrss_child_mb={ru.ru_maxrss/1024:.0f} '
          f'peak_tree_rss_mb={peak[0]/1024:.0f} user_s={ru.ru_utime:.1f} sys_s={ru.ru_stime:.1f} rc={rc}',
          file=sys.stderr, flush=True)
    sys.exit(rc)

main()
