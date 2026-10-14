"""Run a command N times; report wall time and max RSS of the child (stdin/stdout files)."""
import resource, subprocess, sys, time, json
n = int(sys.argv[1]); stdin_path = sys.argv[2]; stdout_path = sys.argv[3]; cmd = sys.argv[4:]
walls = []
for i in range(n):
    with open(stdin_path, "rb") as fi, open(stdout_path, "wb") as fo:
        t0 = time.perf_counter()
        p = subprocess.run(cmd, stdin=fi, stdout=fo, stderr=subprocess.PIPE)
        walls.append(time.perf_counter() - t0)
    sys.stderr.write(p.stderr.decode(errors="replace"))
    if p.returncode:
        sys.exit(p.returncode)
rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(json.dumps({"cmd": " ".join(cmd[:1]), "runs": n, "wall_s": [round(w, 3) for w in walls],
                  "min": round(min(walls), 3), "max": round(max(walls), 3), "max_rss_kb": rss}))
