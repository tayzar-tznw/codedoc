"""Readiness signals when the client does not advertise window.workDoneProgress."""
import asyncio, copy, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start, CLIENT_CAPS
W = "/home/admin/scratch/codedoc-go-research/engine-probe"
async def main():
    caps = copy.deepcopy(CLIENT_CAPS); caps["window"] = {}
    caps["workspace"]["configuration"] = False
    c = make_client(W + "/probe", {}, env_over={"XDG_CONFIG_HOME": W + "/xdgconfig-off"}, caps=caps)
    await start(c)
    await asyncio.sleep(3)
    for e in c.log:
        m = e["msg"]
        if e["dir"] == "in" and "method" in m:
            p = m.get("params") or {}
            print(e["t"], m["method"], ("id=%s" % m["id"]) if "id" in m else "", json.dumps(p)[:160])
    await c.stop()
asyncio.run(main())
