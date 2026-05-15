# Backup sync

This is the design for a system that syncs backups.

Constraints:
* backups can be spread accross multiple locations/computers, but they can also live on a single computer.
* if the backups are on different computers, ssh access is assumed
* ssh can be unidirectional (e.g. A can ssh to B, but B can't ssh to A).
* the sync system does not delete files, it only adds them.
* the sync system syncs multiple directories
* the sync system must be able to restart a sync stopped in the middle.

## Per-file data

Each backup will have a CSV file recording information about all other files in the backup (but not about itself). This CSV file will pad some of the fields in each record with spaces so that we can rewrite them without rewriting the entire file.

Each line of this backup will contain:
id(int;increasing for each file line)
sync-directory(string),
file-path-relative-to-sync-directory(string),
file-state(padded; see below for the exact definition),

The file state is one of "Creating(last-attempt-timestamp: int)" or "Finished(fixed-length-hash)". Whenever a file is being added to the backup, its state becomes "Creating(now)"

