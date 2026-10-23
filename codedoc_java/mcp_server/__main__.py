from mcp_server.config import HOST, PORT
from mcp_server.server import mcp

if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=HOST,
        port=PORT,
    )
