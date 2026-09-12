# Git Drive Backup

An append-only, incremental Git backup system for Google Drive.

Git Drive Backup creates incremental Git bundles and stores them alongside checkpoint metadata in Google Drive. It never uses a Google Drive sync folder, so Git's temporary files and repository churn stay entirely local.

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

Traditional Git repositories do not play particularly nicely with file synchronization services. Git constantly creates, replaces, and removes packfiles, indexes, lock files, temporary objects, and other internal files. A sync client sees a storm of filesystem activity and becomes part of the repository's operational environment.

This project takes a different approach:

> **Git stays Git. Google Drive becomes an archive.**

Instead of synchronizing the `.git` directory, the program asks Git to produce incremental bundles containing objects that have become newly reachable since the previous checkpoint. Those bundles are uploaded as archive objects.

The result is a small, Git-aware backup vault rather than a synchronized Git repository.

## Installation

### Requirements

* Python 3.10 or newer
* Git
* A Google account with Google Drive
* A Google Cloud OAuth client credentials file

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/git-drive-backup.git
cd git-drive-backup
```

Replace `YOUR_USERNAME/git-drive-backup` with the actual repository URL.

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure Google Drive API access

Create a Google Cloud project and enable the **Google Drive API**.

Then configure an OAuth client:

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable the Google Drive API.
4. Configure the OAuth consent screen.
5. Create an OAuth client ID for a **Desktop app**.
6. Download the credentials file.
7. Save it as:

```text
credentials.json
```

Place `credentials.json` in the project root:

```text
git-drive-backup/
├── credentials.json
├── requirements.txt
├── pyproject.toml
└── git_drive_backup/
    └── backup.py
```

The credentials file contains sensitive information. Do not commit it to GitHub.

Add these entries to `.gitignore`:

```gitignore
.venv/
__pycache__/
*.py[cod]
credentials.json
token.json
```

The first time the program runs, it opens a browser window for Google authorization. The resulting token is saved locally as `token.json`.

### 5. Install the command

From the project directory:

```bash
pip install .
```

This installs the `git-drive-backup` command into the active Python environment.

Verify the installation:

```bash
git-drive-backup --help
```

If you want to install it in editable mode while developing:

```bash
pip install -e .
```

### 6. Confirm Git is available

```bash
git --version
```

The program requires Git to create and inspect bundles. The repository being backed up must be a valid Git repository.

## Basic Usage

Back up a repository:

```bash
git-drive-backup /path/to/repository/.git
```

For a normal working-tree repository, the Git directory is usually:

```text
/path/to/repository/.git
```

Run a dry run without uploading anything:

```bash
git-drive-backup --dry-run /path/to/repository/.git
```

List available archives:

```bash
git-drive-backup --list
```

Verify an archive using its original repository:

```bash
git-drive-backup --verify /path/to/repository/.git
```

Verify an archive without the original repository:

```bash
git-drive-backup --verify-archive my-project-a81f23c9d3e4210a
```

## Restoring a Repository

Restore the latest checkpoint into an empty directory:

```bash
git-drive-backup \
    --restore \
    --archive my-project-a81f23c9d3e4210a \
    /path/to/restored-project
```

The destination directory must be empty. The program refuses to overwrite existing files.

Restore a historical checkpoint:

```bash
git-drive-backup \
    --restore \
    --archive my-project-a81f23c9d3e4210a \
    --checkpoint 20260909T191500123456Z-8f31c2a \
    /path/to/restored-project
```

The archive ID is sufficient for recovery. The original repository path, local state file, and original machine are not required.

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

Each checkpoint records:

* The archive ID
* The checkpoint name
* The previous checkpoint
* The complete Git ref map
* The exact symbolic or detached `HEAD` state
* The bundle filename
* The bundle size
* The bundle SHA-256 hash

The checkpoint files form a chain, allowing the archive to detect missing, altered, or incorrectly linked checkpoints.

## Design Philosophy

The backup side is intentionally conservative.

Existing archive objects are never modified or deleted during normal backup operation. A failed upload may leave an orphaned bundle, but it cannot destroy an earlier checkpoint. The next backup can safely continue.

The restore side is equally conservative. The destination must be empty, and the program refuses to overwrite existing files.

The goal is simple:

> Make the backup mechanism less fragile than the data it is protecting.

## License

See `LICENSE`.
