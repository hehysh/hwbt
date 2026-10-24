"""Entry point used by PyInstaller (and convenient for running from a checkout)."""

from __future__ import annotations

import multiprocessing
import sys

from hwmusic.gui.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
