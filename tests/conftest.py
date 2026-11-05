import os
import sys
import tempfile
from pathlib import Path

# Importing app.main builds a default app; keep its database out of the repo.
os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "import.db"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
