"""Console-script entry point for `mthydra-controller`."""
from __future__ import annotations

import sys

from mthydra.controller.cli import run


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
