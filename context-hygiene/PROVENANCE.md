# Provenance: say where a file came from

An agent that reads a summary and trusts it over the file it was built from will state old facts with confidence. The fix is a label on line one of every file, and one rule.

## The three statuses

- **SOURCE.** Primary material: received, downloaded, scanned or written by a person. Agents never edit it.
- **DERIVED.** Written by reading something else. It names what it was built from and when.
- **WORKING.** Drafts and scratch. No authority. Freely rewritten.

## First-line format

Exactly one of these, as the first line of the file:

```
> Status: SOURCE
> Status: DERIVED from <what> · built <date> · re-verify before quoting
> Status: WORKING
```

Example:

```
> Status: DERIVED from docs/api-spec.md · built 2026-10-02 · re-verify before quoting
```

The `·` is a middle dot. Any date format works if you keep to one. Files that cannot carry a first line (JSON, images, binaries) get a label in the README of their folder instead.

## The rule

**A DERIVED file never outranks its SOURCE. When they disagree, the SOURCE wins, without discussion.**

In practice: when a path, a date, a status or a number matters, open the file instead of recalling it. Read the whole folder instead of guessing what is in it. Thinking harder about a wrong premise gives a more confident wrong answer.

## Keeping labels honest

- Mark staleness with a banner. Never silently rewrite a summary to match the source. A quietly corrected summary is how a wrong one becomes authoritative.
- Add labels one file at a time, after checking what the file is. A wrong label is worse than none.
- Put the convention into your `CLAUDE.md` or `AGENTS.md` so the agent reads it at the start of every session. The inventory script prints a one-line reminder of it too.
