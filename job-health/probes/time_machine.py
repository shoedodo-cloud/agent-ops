"""time_machine: when did the last Time Machine backup complete? macOS only.

Config keys:
  max_age_hours  how old the latest completed backup may be. Default 24.

macOS runs Time Machine on its own schedule and tells no one when it stops.
This row makes the gap visible.

OK     a completed backup newer than max_age_hours.
STALE  older than that, no destination, destination not attached, tmutil
       silent (it may need Full Disk Access for your terminal), or not macOS.
       "I cannot see a backup" is never reported as "no backup exists".
"""

import os
import re
import subprocess
import sys
from typing import List, Tuple

from job_health import (DEFAULT_MAX_AGE_H, STALE, Health, _age_hours, classify,
                        ts_tm_path)

TMUTIL = "/usr/bin/tmutil"


def _run(cmd: List[str], timeout: int = 20) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return (p.returncode,
            (p.stdout or b"").decode("utf-8", "replace").strip(),
            (p.stderr or b"").decode("utf-8", "replace").strip())


def probe(job: dict) -> Health:
    name = job.get("name", "")
    max_age = float(job.get("max_age_hours", DEFAULT_MAX_AGE_H))
    if sys.platform != "darwin":
        return Health(name, "", STALE, None,
                      "time_machine is macOS-only, this is %s" % sys.platform)
    if not os.path.exists(TMUTIL):
        return Health(name, "", STALE, None, "tmutil not found at %s" % TMUTIL)

    _rc, path, _err = _run([TMUTIL, "latestbackup"])
    if not path:
        _rc, out, err = _run([TMUTIL, "destinationinfo"])
        info = (out + "\n" + err).strip()
        if re.search(r"no destinations?", info, re.I):
            return Health(name, "", STALE, None, "no Time Machine destination is configured")
        if not info:
            return Health(name, "", STALE, None,
                          "tmutil gave no answer (Full Disk Access?), cannot tell")
        if "Mount Point" not in info:
            return Health(name, "", STALE, None,
                          "destination not attached, so the last backup time cannot be read",
                          "attach the backup disk, then re-check")

    age = _age_hours(ts_tm_path(path))
    ok = True if path else None          # a path at all == a completed backup exists
    state = classify(age, max_age, ok)
    if not path:
        outcome = "destination attached, but no completed backup found"
    elif age is None:
        outcome = "%s   <- no date could be read from this path" % os.path.basename(path)
    else:
        outcome = os.path.basename(path)
    return Health(name, "", state, age, outcome)
