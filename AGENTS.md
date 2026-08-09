# Pandrator repository guidance

Keep changes scoped to the requested component. Preserve unrelated working-tree
changes and untracked user files; inspect the status and nearby patterns before
editing. Use the focused checks for the changed area, and report commands that
were run plus known limits rather than claiming broad project cleanliness.

## Graphify policy

Graphify is opt-in only. For routine code questions, use source inspection,
`rg`, CI configuration, and Git history first; do not run Graphify commands,
watchers, hooks, global or database pushes, or URL ingestion.

When Graphify is explicitly requested, respect `.graphifyignore`, write output
outside the repository or under the relevant component only, and verify any
conclusions against the source before relying on them.
