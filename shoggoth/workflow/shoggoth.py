#!/usr/bin/env python3
"""Shoggoth workflow + maintenance: dispatch entrypoint shim.

The implementation lives in the ``shoggoth`` package alongside this
file; this shim forwards to ``shoggoth.main()``, so invoking
``python3 /shoggoth/workflow/scripts/shoggoth.py <command>`` reaches
the package's CLI.

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
