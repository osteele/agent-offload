# /// script
# requires-python = ">=3.10"
# [tool.weft]
# isolated = true
# ///

from __future__ import annotations

import os
import sys


def main() -> None:
    os.execvp("agent-offload-worker", ["agent-offload-worker", *sys.argv[1:]])


if __name__ == "__main__":
    main()
