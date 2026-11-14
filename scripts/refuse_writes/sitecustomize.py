"""For scripts/check_synconly.py only (CHECK_REFUSE_WRITES): the desktop's folder sync finds some
of its file writes refused, at random, as on Windows when another program (an antivirus scan, an
indexer, an editor) has the file open. The sync must try them again and still end in step.

Loaded through PYTHONPATH in the desktop processes the check starts, and only there."""
import os
import pathlib
import random

if os.environ.get("TEXLOOM_CHECKS") and os.environ.get("CHECK_REFUSE_WRITES"):
    _replace = pathlib.Path.replace
    _rate = float(os.environ["CHECK_REFUSE_WRITES"])

    def _refusing(self, target):
        if ".texloom-tmp" in self.name and random.random() < _rate:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process (CHECK_REFUSE_WRITES)")
        return _replace(self, target)

    pathlib.Path.replace = _refusing
