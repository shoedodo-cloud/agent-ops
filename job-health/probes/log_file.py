"""log_file: judge a job by the last stamped line of its log.

Config keys:
  path           the log file. `~` and $VARS are expanded. Required.
  ok_regex       a line matching this reports success. Required.
  fail_regex     a line matching this reports failure. Checked first. Optional.
  timestamp      auto | iso_z | bracketed_iso | space_local. Default auto.
  max_age_hours  how old the last stamped line may be. Default 24.
  detail         free text shown in `probe <name>`. Optional.

Only the last 256 KB of the file is read. The row is STALE when the file is
missing, when no line carries a readable timestamp, when the last stamped line
matches neither pattern, or when the config cannot be used.
"""

import os
import re

from job_health import (DEFAULT_MAX_AGE_H, STALE, TS_READERS, Health,
                        health_from_log, outcome_from_regex)

TAIL_BYTES = 256 * 1024


def _tail(path: str) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        return f.read().decode("utf-8", "replace")


def probe(job: dict) -> Health:
    name = job.get("name", "")
    path = job.get("path")
    if not path:
        return Health(name, "", STALE, None, "log_file: no `path` in config")
    if not job.get("ok_regex"):
        return Health(name, "", STALE, None,
                      "log_file: no `ok_regex` in config, so success cannot be recognised")
    fmt = job.get("timestamp", "auto")
    reader = TS_READERS.get(fmt)
    if reader is None:
        return Health(name, "", STALE, None, "log_file: unknown timestamp format %r (use %s)"
                      % (fmt, ", ".join(sorted(TS_READERS))))
    try:
        outcome = outcome_from_regex(job.get("ok_regex"), job.get("fail_regex"))
    except re.error as e:
        return Health(name, "", STALE, None, "log_file: bad regex in config: %s" % e)

    full = os.path.expanduser(os.path.expandvars(path))
    try:
        text = _tail(full)
    except FileNotFoundError:
        return Health(name, "", STALE, None, "log not found: %s" % full)
    except OSError as e:
        return Health(name, "", STALE, None, "log unreadable: %s: %s" % (full, e))

    max_age = float(job.get("max_age_hours", DEFAULT_MAX_AGE_H))
    row = health_from_log(name, "", text, max_age, reader, outcome,
                          detail=str(job.get("detail") or ""))
    if row.outcome == "no log" and text.strip():
        lines = len([l for l in text.splitlines() if l.strip()])
        row = row._replace(outcome="%d line(s), none with a readable `%s` timestamp"
                           % (lines, fmt))
    return row
