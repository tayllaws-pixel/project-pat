# Make the monorepo packages importable without an install step (also mirrors pyproject pythonpath).
import os, sys
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_root, os.path.join(_root, "services/shared"), os.path.join(_root, "services/sentinel"), os.path.join(_root, "infra")):
    if p not in sys.path:
        sys.path.insert(0, p)
