"""`argbot lab ...` entry point, also callable as main([...]) with the lab arguments only."""
from argbot.cli import main as _main


def main(argv=None):
    import sys
    return _main(["lab", *(sys.argv[1:] if argv is None else argv)])
