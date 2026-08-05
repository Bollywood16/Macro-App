# Market Memory — working conventions

## Output convention

Any report, audit, investigation, or analysis longer than ~10 lines goes in a file under
`docs/` or `REPORTS/`. The terminal gets at most 5 lines: the headline finding and the
file path. Never print a long report inline — it has to be moved between tools by hand
and gets truncated.

Commit the file in the same turn it's written. Uncommitted docs get lost.
