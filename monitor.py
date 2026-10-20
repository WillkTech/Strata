#!/usr/bin/env python3
"""Strata terminal monitor compatibility entry point.

Delegates to `strata_cli.monitor_commands` to run the canonical monitor.
"""

import sys
from strata_cli.monitor_commands import main

if __name__ == "__main__":
    sys.exit(main(["monitor"] + sys.argv[1:]))
