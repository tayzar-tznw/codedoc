"""Report gopls child processes after startup (telemetry sidecar check)."""
import asyncio, os, subprocess, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import make_client, start

async def main():
    env = {"XDG_CONFIG_HOME": sys.argv[2]}
    c = make_client(sys.argv[1], {}, env_over=env)
    await start(c)
    await asyncio.sleep(2)
    pid = c._proc.pid
    out = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid,args"], capture_output=True, text=True).stdout
    print("XDG_CONFIG_HOME", sys.argv[2], "children:", out.strip().splitlines()[1:] or "none")
    await c.stop()

asyncio.run(main())
