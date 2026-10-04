"""Entry point for action.yml, which runs it as python3 -I run.py.

Isolated mode drops the script's directory from sys.path on Python 3.11 and
newer, so it is added back here, first, and nothing else is.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from post_no_bills.main import main  # noqa: E402

raise SystemExit(main())
