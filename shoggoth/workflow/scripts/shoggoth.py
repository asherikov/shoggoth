#!/usr/bin/env python3
"""Shoggoth workflow + maintenance: dispatch entrypoint shim.

Originally a single 7.5k-line script; the implementation now lives
in the ``shoggoth`` package alongside this file. Invoking
``python3 /shoggoth/workflow/scripts/shoggoth.py <command>``
keeps working unchanged — this shim forwards to ``shoggoth.main()``.

The argparse choices and the exit codes are all defined inside
the package, so any drift between a caller's expected surface
and the package's actual surface surfaces immediately as an
argparse error rather than as a silent ``AttributeError`` at
dispatch time.
"""
import sys

from shoggoth.cli import main

if __name__ == "__main__":
    sys.exit(main())
