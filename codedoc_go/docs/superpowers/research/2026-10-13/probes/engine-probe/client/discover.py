"""Discovery run: initialize gopls on a folder and log every message for N seconds."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start, dump_log, progress_events, rss_kb

async def main():
    folder, out, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
    settings = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
    c = make_client(folder, settings, stderr_path=out + ".stderr")
    res, dt = await start(c)
    json.dump(res, open(out + ".initialize.json", "w"), indent=1)
    print("initialize round trip %.3fs" % dt)
    await asyncio.sleep(secs)
    print("rss_kb", rss_kb(c._proc.pid))
    await c.stop()
    dump_log(c, out + ".log.jsonl")
    for p in progress_events(c):
        print(p)

asyncio.run(main())
