"""git_repo: is a checkout stuck, or holding work nobody else can see?

Config keys:
  path           the work tree. `~` and $VARS are expanded. Required.
  max_age_hours  how old the oldest unpushed commit may be. Default 26.

FAILING  mid-rebase, mid-merge, unresolved conflicts, or unpushed work older
         than max_age_hours.
OK       clean, or unpushed work younger than that. Being behind is reported,
         not graded.
STALE    no such path, not a git work tree, no upstream, or output that does
         not parse.

Local only. It does not fetch, so "behind" is as of the last fetch.
"""

import os

from job_health import STALE, UNPUSHED_MAX_H, Health, probe_git


def probe(job: dict) -> Health:
    name = job.get("name", "")
    path = job.get("path")
    if not path:
        return Health(name, "", STALE, None, "git_repo: no `path` in config")
    return probe_git(name, os.path.expanduser(os.path.expandvars(path)),
                     float(job.get("max_age_hours", UNPUSHED_MAX_H)))
