# Next release

These changes are in development and are not included in the published 0.14.4
release.

## Performance

A revision's detail repeats less work for cast segments. The audio identity
check reuses the cast freeze's parse of the same speech XML, and retained voice
checks share one provider registry. On a 5,167-segment cast book, the detail
read went from 6.55 s to 5.85 s, measured by Patrick Nadeau (#146).

## Manager

Restarting Pandrator through the Manager's runtime API or `pandrator-manager
runtime-restart` after an update now runs the newly activated version.
Previously the API, worker and MCP came back on the previous slot until they
were stopped and started again. When the launch configuration has changed, a
restart that includes the API or a worker first stops every running Pandrator
service, so old and new processes never share the database, then starts them
all on the active slot. Without such a change, a restart still affects only the
selected services. This ships with the next Manager release.
