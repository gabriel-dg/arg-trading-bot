"""All tests are offline: brokers are simulated (paper broker or httpx.MockTransport)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
