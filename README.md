# Git Google Drive Backup

An append-only, incremental Git backup system for Google Drive.

Git Google Drive Backup creates incremental Git bundles and stores them alongside tamper-evident checkpoint metadata in Google Drive. It never uses a Google Drive sync folder, so Git's temporary files and repository churn stay entirely local.

Each repository gets its own self-identifying archive, allowing a complete Git repository to be restored into an empty directory without access to the original repository or machine.

## Features

* Append-only Google Drive backups
* Incremental Git bundles instead of full repository copies
* No Git repository stored inside a Google Drive sync folder
* Self-identifying archives with stable archive IDs
* Complete Git ref preservation, including tags and remote-tracking refs
* Explicit preservation of symbolic and detached `HEAD`
* SHA-256 verification of every backup bundle
* Cryptographically chained checkpoint metadata
* Archive integrity verification without the original repository
* Restore to a completely empty directory
* Restore from any historical checkpoint
* Refuses to overwrite an existing restore destination
* No local backup state file required
* Multiple independent repositories supported
* Simple command-line interface

## Why?

Traditional Git repositories don't play particularly nicely with file synchronization services. Git constantly creates, replaces, and removes packfiles, indexes, lock files, temporary objects, and other internal files. A sync client sees a storm of filesystem activity and becomes part of the repository's operational environment.

This project takes a different approach:

**Git stays Git. Google Drive becomes an archive.**

Instead of synchronizing the `.git` directory, the program asks Git to produce incremental bundles containing everything that has become newly reachable since the previous checkpoint. Those bundles are uploaded as immutable archive objects.

The result is effectively a small, Git-aware backup vault rather than a synchronized Git repository.

## Archive Structure

```text
Google Drive/
└── Git Backups/
    └── my-project-a81f23c9d3e4210a/
        ├── archive.json
        ├── 20260909T191500123456Z-8f31c2a.bundle
        ├── 20260909T191500123456Z-8f31c2a.state.json
        ├── 20260910T081200654321Z-91ac442.bundle
        ├── 20260910T081200654321Z-91ac442.state.json
        └── ...
```

Every checkpoint records the repository's refs and `HEAD`, the preceding checkpoint, and the SHA-256 hash and size of its bundle.

The archive therefore contains everything necessary to walk its history forward and reconstruct the repository.

## Basic Usage

Back up a repository:

```bash
git-drive-backup /path/to/repository/.git
```

Verify an archive:

```bash
git-drive-backup --verify-archive my-project-a81f23c9d3e4210a
```

List available archives:

```bash
git-drive-backup --list
```

Restore into an empty directory:

```bash
git-drive-backup \
    --restore \
    --archive my-project-a81f23c9d3e4210a \
    /path/to/restored-project
```

Restore a historical checkpoint:

```bash
git-drive-backup \
    --restore \
    --archive my-project-a81f23c9d3e4210a \
    --checkpoint 20260909T191500123456Z-8f31c2a \
    /path/to/restored-project
```

## Design Philosophy

The backup side is intentionally conservative.

Existing archive objects are never modified or deleted during normal backup operation. A failed upload can leave an orphaned bundle, but it cannot destroy an earlier checkpoint. The next backup can safely continue.

The restore side is equally conservative. The destination must be empty, and the program refuses to overwrite existing files.

The goal is simple:

> Make the backup mechanism less fragile than the data it is protecting.

## Requirements

* Python 3.10+
* Git
* Google Drive API access
* A Google OAuth client credentials file

See the project documentation for Google Cloud setup and installation instructions.

## License

See `LICENSE`.
