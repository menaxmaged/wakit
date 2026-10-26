"""Entry point of the standalone wakit app: double-clicked (no arguments) it opens the GUI, otherwise it is the CLI."""
import sys

from wakit.cli import main

if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # the GUI shows a job's output as it happens
    main(sys.argv[1:] or ["gui"])
