"""Setup that has to happen before any test module imports main."""

import os
import sys

# main reads GITHUB_STEP_SUMMARY when it is imported.
os.environ.setdefault("GITHUB_STEP_SUMMARY", "/tmp/step_summary.txt")

# main.py sits at the repository root, one level above this directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
