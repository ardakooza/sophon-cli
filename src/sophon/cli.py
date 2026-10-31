"""Installed console-script entry point; behavior lives in the shared core."""

from sophon.core import main

app = main


if __name__ == "__main__":
    raise SystemExit(main())
