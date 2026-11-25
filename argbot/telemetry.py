"""Local cycle evidence only; never a broker input."""
import json
import os
from pathlib import Path


def append_cycle(directory, record):
    # Each line is independently parseable. A truncated last line after a hard
    # crash is evidence of an incomplete write, never a successful cycle.
    encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True)
    with (Path(directory) / 'operational-cycles.private.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(encoded + '\n')
        stream.flush()
        os.fsync(stream.fileno())
