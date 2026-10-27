import logging
import sys

import uvicorn

from .app import create_app
from .config import Catalog, load_config


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if len(sys.argv) == 3 and sys.argv[1] == "check":
        Catalog.read(sys.argv[2])
        print("Catalog is valid.")
        return
    cfg = load_config()
    uvicorn.run(create_app(cfg), host="0.0.0.0", port=cfg.port, timeout_keep_alive=95, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
