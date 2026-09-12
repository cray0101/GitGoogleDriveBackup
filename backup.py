#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]

DRIVE_ROOT_NAME = "Git Backups"

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"
TOKEN_FILE = PROJECT_DIR / "token.json"

STATE_SUFFIX = ".state.json"
BUNDLE_SUFFIX = ".bundle"

ARCHIVE_FORMAT_VERSION = 2


# ---------------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)

    return h.hexdigest()


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:

    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=capture_output,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {args[0]}")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        stdout = (e.stdout or "").strip()

        details = stderr or stdout or f"exit code {e.returncode}"

        raise RuntimeError(
            f"Command failed: {' '.join(args)}\n{details}"
        ) from e


def run_git(
    git_dir: Path,
    args: list[str],
    *,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:

    return run_command(
        ["git", f"--git-dir={git_dir}", *args],
        capture_output=capture_output,
    )


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )

    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=2,
                sort_keys=True,
            )
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)

    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Git repository inspection
# ---------------------------------------------------------------------------

def verify_git_repository(git_dir: Path) -> None:
    git_dir = git_dir.resolve()

    if not git_dir.exists():
        raise RuntimeError(f"Git directory does not exist: {git_dir}")

    result = run_git(
        git_dir,
        ["rev-parse", "--git-dir"],
    )

    if result.stdout.strip() != str(git_dir):
        # Git may normalize the path differently, so this is deliberately
        # only a sanity check rather than a strict equality requirement.
        pass


def get_repository_name(git_dir: Path) -> str:
    """
    Obtain the repository name from the common Git directory location.

    For a normal repository:
        /foo/project/.git -> project

    For a bare repository:
        /foo/project.git -> project
    """

    name = git_dir.name

    if name == ".git":
        return git_dir.parent.name

    if name.endswith(".git"):
        name = name[:-4]

    return name or "repository"


def repository_identity(git_dir: Path) -> tuple[str, str]:
    """
    Generate a stable archive ID.

    The absolute Git directory path is intentionally incorporated into the
    ID so two different local repositories with the same repository name
    cannot accidentally share an archive.
    """

    git_dir = git_dir.resolve()
    repository_name = get_repository_name(git_dir)

    digest = hashlib.sha256(
        str(git_dir).encode("utf-8")
    ).hexdigest()[:16]

    safe_name = "".join(
        c if c.isalnum() or c in "-_." else "-"
        for c in repository_name
    )

    archive_id = f"{safe_name}-{digest}"

    return archive_id, repository_name


def get_head(git_dir: Path) -> dict[str, str]:
    """
    Return HEAD explicitly as either:

        {"type": "symbolic", "ref": "refs/heads/main"}

    or:

        {"type": "detached", "sha": "..."}
    """

    symbolic = run_git(
        git_dir,
        ["symbolic-ref", "-q", "HEAD"],
    )

    if symbolic.returncode == 0:
        ref = symbolic.stdout.strip()

        if not ref:
            raise RuntimeError("Git returned an empty symbolic HEAD")

        return {
            "type": "symbolic",
            "ref": ref,
        }

    sha = run_git(
        git_dir,
        ["rev-parse", "HEAD"],
    ).stdout.strip()

    if not sha:
        raise RuntimeError("Unable to determine HEAD")

    return {
        "type": "detached",
        "sha": sha,
    }


def get_all_refs(git_dir: Path) -> dict[str, str]:
    """
    Capture every direct Git ref.

    This deliberately uses for-each-ref rather than show-ref so annotated
    tags, notes, remote-tracking refs, etc. are retained.
    """

    result = run_git(
        git_dir,
        [
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ],
    )

    refs: dict[str, str] = {}

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            ref, sha = line.split(" ", 1)
        except ValueError:
            raise RuntimeError(
                f"Unable to parse Git ref: {line!r}"
            )

        refs[ref] = sha

    return dict(sorted(refs.items()))


# ---------------------------------------------------------------------------
# Bundle creation
# ---------------------------------------------------------------------------

def create_incremental_bundle(
    git_dir: Path,
    output_path: Path,
    previous_refs: dict[str, str] | None,
) -> None:
    """
    Create a Git bundle containing objects reachable from the current
    repository refs but not already reachable from the previous checkpoint.

    Existing archive bundles are never modified.
    """

    current_refs = get_all_refs(git_dir)

    args = [
        "bundle",
        "create",
        str(output_path),
        "--all",
    ]

    previous_objects = sorted(set(previous_refs.values())) if previous_refs else []

    for sha in previous_objects:
        args.extend(["--not", sha])

    run_git(
        git_dir,
        args,
        capture_output=True,
    )


def verify_bundle(bundle_path: Path) -> None:
    run_command(
        ["git", "bundle", "verify", str(bundle_path)],
    )


def bundle_has_objects(bundle_path: Path) -> bool:
    """
    Determine whether a bundle actually contains commits/objects.

    A bundle containing only the header is not useful as an incremental
    checkpoint.
    """

    result = run_command(
        ["git", "bundle", "list-heads", str(bundle_path)],
    )

    return bool(result.stdout.strip())


# ---------------------------------------------------------------------------
# Google Drive authentication
# ---------------------------------------------------------------------------

def get_credentials() -> Credentials:
    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise RuntimeError(
                f"Missing Google OAuth credentials file:\n"
                f"  {CREDENTIALS_FILE}\n\n"
                f"Download OAuth client credentials from Google Cloud "
                f"and save them as credentials.json."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            SCOPES,
        )

        creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(
            creds.to_json(),
            encoding="utf-8",
        )

    return creds


def get_drive():
    creds = get_credentials()

    return build(
        "drive",
        "v3",
        credentials=creds,
    )


# ---------------------------------------------------------------------------
# Google Drive operations
# ---------------------------------------------------------------------------

def escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_drive_files(
    drive,
    *,
    parent_id: str | None = None,
    name: str | None = None,
    mime_type: str | None = None,
) -> list[dict[str, Any]]:

    conditions = [
        "trashed = false",
    ]

    if parent_id:
        conditions.append(
            f"'{escape_drive_query(parent_id)}' in parents"
        )

    if name is not None:
        conditions.append(
            f"name = '{escape_drive_query(name)}'"
        )

    if mime_type is not None:
        conditions.append(
            f"mimeType = '{escape_drive_query(mime_type)}'"
        )

    query = " and ".join(conditions)

    files: list[dict[str, Any]] = []
    page_token = None

    while True:
        response = drive.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id,name,mimeType,size,modifiedTime)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()

        files.extend(response.get("files", []))

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return files


def find_unique_file(
    drive,
    *,
    parent_id: str,
    name: str,
) -> dict[str, Any] | None:

    files = list_drive_files(
        drive,
        parent_id=parent_id,
        name=name,
    )

    if len(files) > 1:
        raise RuntimeError(
            f"Multiple Drive files named {name!r} exist in the same "
            f"archive. Refusing to guess."
        )

    return files[0] if files else None


def find_or_create_folder(
    drive,
    name: str,
    parent_id: str | None = None,
) -> dict[str, Any]:

    files = list_drive_files(
        drive,
        parent_id=parent_id,
        name=name,
        mime_type="application/vnd.google-apps.folder",
    )

    if len(files) > 1:
        raise RuntimeError(
            f"Multiple Drive folders named {name!r} exist. "
            f"Refusing to guess."
        )

    if files:
        return files[0]

    metadata: dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }

    if parent_id:
        metadata["parents"] = [parent_id]

    return drive.files().create(
        body=metadata,
        fields="id,name,mimeType",
    ).execute()


def get_root_folder(drive) -> dict[str, Any]:
    return find_or_create_folder(
        drive,
        DRIVE_ROOT_NAME,
    )


def get_or_create_backup_folder(
    drive,
    archive_id: str,
) -> dict[str, Any]:

    root = get_root_folder(drive)

    return find_or_create_folder(
        drive,
        archive_id,
        root["id"],
    )


def find_backup_folder(
    drive,
    archive_id: str,
) -> dict[str, Any]:

    root_files = list_drive_files(
        drive,
        name=DRIVE_ROOT_NAME,
        mime_type="application/vnd.google-apps.folder",
    )

    if len(root_files) > 1:
        raise RuntimeError(
            f"Multiple '{DRIVE_ROOT_NAME}' folders exist in Drive. "
            f"Refusing to guess."
        )

    if not root_files:
        raise RuntimeError(
            f"Drive archive root '{DRIVE_ROOT_NAME}' does not exist."
        )

    folders = list_drive_files(
        drive,
        parent_id=root_files[0]["id"],
        name=archive_id,
        mime_type="application/vnd.google-apps.folder",
    )

    if len(folders) > 1:
        raise RuntimeError(
            f"Multiple archive folders named {archive_id!r} exist."
        )

    if not folders:
        raise RuntimeError(
            f"Archive not found: {archive_id}"
        )

    return folders[0]


def upload_file(
    drive,
    local_path: Path,
    *,
    parent_id: str,
    name: str | None = None,
) -> dict[str, Any]:

    filename = name or local_path.name

    metadata = {
        "name": filename,
        "parents": [parent_id],
    }

    media = MediaFileUpload(
        str(local_path),
        resumable=True,
    )

    return drive.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,size,modifiedTime",
    ).execute()


def download_drive_file(
    drive,
    file_id: str,
) -> bytes:

    request = drive.files().get(
        fileId=file_id,
        alt="media",
    )

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buffer,
        request,
    )

    done = False

    while not done:
        _, done = downloader.next_chunk()

    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Archive manifest
# ---------------------------------------------------------------------------

def make_archive_manifest(
    *,
    archive_id: str,
    repository_name: str,
    git_dir: Path,
) -> dict[str, Any]:

    return {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "archive_id": archive_id,
        "repository_name": repository_name,
        "created_at": utc_now(),

        # Informational only. Restore does NOT depend on this path.
        "original_git_directory": str(git_dir.resolve()),
    }


def load_archive_manifest(
    drive,
    archive_folder: dict[str, Any],
) -> dict[str, Any]:

    manifest_file = find_unique_file(
        drive,
        parent_id=archive_folder["id"],
        name="archive.json",
    )

    if not manifest_file:
        raise RuntimeError(
            f"Archive {archive_folder['name']} has no archive.json."
        )

    data = download_drive_file(
        drive,
        manifest_file["id"],
    )

    try:
        manifest = json.loads(data.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(
            "archive.json is not valid JSON."
        ) from e

    if manifest.get("format_version") != ARCHIVE_FORMAT_VERSION:
        raise RuntimeError(
            f"Unsupported archive format version: "
            f"{manifest.get('format_version')}"
        )

    if manifest.get("archive_id") != archive_folder["name"]:
        raise RuntimeError(
            "Archive manifest ID does not match its Drive folder."
        )

    return manifest


def create_archive_if_needed(
    drive,
    archive_id: str,
    repository_name: str,
    git_dir: Path,
) -> dict[str, Any]:

    folder = get_or_create_backup_folder(
        drive,
        archive_id,
    )

    manifest_file = find_unique_file(
        drive,
        parent_id=folder["id"],
        name="archive.json",
    )

    if manifest_file:
        manifest = load_archive_manifest(
            drive,
            folder,
        )

        if manifest["archive_id"] != archive_id:
            raise RuntimeError(
                "Archive identity mismatch."
            )

        return folder

    manifest = make_archive_manifest(
        archive_id=archive_id,
        repository_name=repository_name,
        git_dir=git_dir,
    )

    with tempfile.TemporaryDirectory() as temp:
        manifest_path = Path(temp) / "archive.json"

        atomic_write_json(
            manifest_path,
            manifest,
        )

        upload_file(
            drive,
            manifest_path,
            parent_id=folder["id"],
            name="archive.json",
        )

    return folder


# ---------------------------------------------------------------------------
# State/checkpoint handling
# ---------------------------------------------------------------------------

def checkpoint_name(timestamp: str, head: dict[str, str]) -> str:
    if head["type"] == "symbolic":
        # The SHA is unavailable without dereferencing HEAD, so the caller
        # should provide a deterministic suffix separately when needed.
        suffix = "head"
    else:
        suffix = head["sha"][:12]

    safe_timestamp = (
        timestamp
        .replace(":", "")
        .replace("+", "")
    )

    return f"{safe_timestamp}-{suffix}"


def state_filename(checkpoint: str) -> str:
    return f"{checkpoint}{STATE_SUFFIX}"


def bundle_filename(checkpoint: str) -> str:
    return f"{checkpoint}{BUNDLE_SUFFIX}"


def find_state_files(
    drive,
    archive_folder: dict[str, Any],
) -> list[dict[str, Any]]:

    files = list_drive_files(
        drive,
        parent_id=archive_folder["id"],
    )

    return [
        f for f in files
        if f["name"].endswith(STATE_SUFFIX)
    ]


def find_latest_state_file(
    drive,
    archive_folder: dict[str, Any],
) -> dict[str, Any] | None:

    states = find_state_files(
        drive,
        archive_folder,
    )

    if not states:
        return None

    # We don't trust Drive modifiedTime as part of the archive's logical
    # ordering. Checkpoint filenames are chronological and the state chain
    # below is authoritative.
    states.sort(key=lambda x: x["name"])

    return states[-1]


def load_state_file(
    drive,
    state_file: dict[str, Any],
) -> dict[str, Any]:

    data = download_drive_file(
        drive,
        state_file["id"],
    )

    try:
        state = json.loads(data.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(
            f"Invalid JSON in {state_file['name']}"
        ) from e

    validate_state(state)

    return state


def validate_state(state: dict[str, Any]) -> None:

    required = [
        "format_version",
        "archive_id",
        "checkpoint",
        "created_at",
        "previous_state",
        "bundle",
        "refs",
        "head",
    ]

    for key in required:
        if key not in state:
            raise RuntimeError(
                f"State file is missing required field: {key}"
            )

    if state["format_version"] != ARCHIVE_FORMAT_VERSION:
        raise RuntimeError(
            f"Unsupported state format version: "
            f"{state['format_version']}"
        )

    if not isinstance(state["refs"], dict):
        raise RuntimeError("State refs must be an object.")

    if not isinstance(state["head"], dict):
        raise RuntimeError("State HEAD must be an object.")

    head_type = state["head"].get("type")

    if head_type == "symbolic":
        if not state["head"].get("ref"):
            raise RuntimeError(
                "Symbolic HEAD has no ref."
            )

    elif head_type == "detached":
        if not state["head"].get("sha"):
            raise RuntimeError(
                "Detached HEAD has no SHA."
            )

    else:
        raise RuntimeError(
            f"Invalid HEAD type: {head_type!r}"
        )


def get_state_files_in_chain(
    drive,
    archive_folder: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:

    state_files = find_state_files(
        drive,
        archive_folder,
    )

    if not state_files:
        return []

    by_name = {
        f["name"]: f
        for f in state_files
    }

    # There must be exactly one terminal state, meaning exactly one state
    # that is not referenced by another state.
    referenced: set[str] = set()

    loaded: dict[str, dict[str, Any]] = {}

    for file in state_files:
        state = load_state_file(
            drive,
            file,
        )

        loaded[file["name"]] = state

        previous = state["previous_state"]

        if previous:
            referenced.add(previous)

    terminals = [
        name
        for name in by_name
        if name not in referenced
    ]

    if len(terminals) != 1:
        raise RuntimeError(
            f"Archive state chain is ambiguous or corrupted. "
            f"Found {len(terminals)} terminal states."
        )

    chain: list[tuple[dict[str, Any], dict[str, Any]]] = []

    current_name = terminals[0]
    seen: set[str] = set()

    while current_name:
        if current_name in seen:
            raise RuntimeError(
                "State chain contains a cycle."
            )

        seen.add(current_name)

        if current_name not in by_name:
            raise RuntimeError(
                f"State chain references missing state: {current_name}"
            )

        file = by_name[current_name]
        state = loaded[current_name]

        chain.append((file, state))

        current_name = state["previous_state"]

    chain.reverse()

    return chain


def validate_checkpoint_chain(
    drive,
    archive_folder: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:

    chain = get_state_files_in_chain(
        drive,
        archive_folder,
    )

    previous_state_sha: str | None = None

    for state_file, state in chain:

        if state["archive_id"] != archive_folder["name"]:
            raise RuntimeError(
                f"State {state_file['name']} belongs to a different archive."
            )

        if state["previous_state"] != previous_state_sha:
            raise RuntimeError(
                f"Broken state chain at {state_file['name']}."
            )

        previous_state_sha = sha256_bytes(
            json.dumps(
                state,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
        )

    return chain


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup(git_dir: Path, *, dry_run: bool = False) -> None:

    git_dir = git_dir.resolve()

    verify_git_repository(git_dir)

    archive_id, repository_name = repository_identity(
        git_dir
    )

    print(f"Repository : {repository_name}")
    print(f"Git dir    : {git_dir}")
    print(f"Archive ID : {archive_id}")

    drive = get_drive()

    if dry_run:
        archive_folder = None
    else:
        archive_folder = create_archive_if_needed(
            drive,
            archive_id,
            repository_name,
            git_dir,
        )

    previous_state: dict[str, Any] | None = None

    if archive_folder:
        latest = find_latest_state_file(
            drive,
            archive_folder,
        )

        if latest:
            previous_state = load_state_file(
                drive,
                latest,
            )

    current_refs = get_all_refs(git_dir)
    current_head = get_head(git_dir)

    if previous_state:
        if (
            previous_state["refs"] == current_refs
            and previous_state["head"] == current_head
        ):
            print("Nothing new to back up.")
            return

    timestamp = utc_now()

    # Use HEAD's resolved SHA for the checkpoint name. This keeps names
    # useful even when HEAD is symbolic.
    head_sha = run_git(
        git_dir,
        ["rev-parse", "HEAD"],
    ).stdout.strip()

    safe_timestamp = (
        timestamp
        .replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("Z", "Z")
    )

    checkpoint = f"{safe_timestamp}-{head_sha[:12]}"

    bundle_name = bundle_filename(checkpoint)
    state_name = state_filename(checkpoint)

    previous_refs = (
        previous_state["refs"]
        if previous_state
        else None
    )

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)

        bundle_path = temp_dir / bundle_name

        print(f"Creating bundle: {bundle_name}")

        create_incremental_bundle(
            git_dir,
            bundle_path,
            previous_refs,
        )

        verify_bundle(bundle_path)

        if not bundle_has_objects(bundle_path):
            print("Git reported no new bundle heads.")
            return

        bundle_sha = sha256_file(bundle_path)
        bundle_size = bundle_path.stat().st_size

        previous_state_name = (
            state_name_for_state(previous_state)
            if previous_state
            else None
        )

        state = {
            "format_version": ARCHIVE_FORMAT_VERSION,
            "archive_id": archive_id,
            "checkpoint": checkpoint,
            "created_at": timestamp,

            "previous_state": previous_state_name,

            "bundle": {
                "filename": bundle_name,
                "sha256": bundle_sha,
                "size": bundle_size,
            },

            "refs": current_refs,
            "head": current_head,
        }

        state_path = temp_dir / state_name

        atomic_write_json(
            state_path,
            state,
        )

        if dry_run:
            print()
            print("DRY RUN")
            print(f"Would upload: {bundle_name}")
            print(f"Would upload: {state_name}")
            return

        # Important ordering:
        #
        #   1. Bundle
        #   2. State
        #
        # If state upload fails, the bundle is simply an orphaned immutable
        # object. The next backup can safely create another bundle.
        print("Uploading bundle...")
        upload_file(
            drive,
            bundle_path,
            parent_id=archive_folder["id"],
            name=bundle_name,
        )

        print("Uploading state...")
        upload_file(
            drive,
            state_path,
            parent_id=archive_folder["id"],
            name=state_name,
        )

    print("Backup complete.")
    print(f"Archive: {archive_id}")
    print(f"Checkpoint: {checkpoint}")


def state_name_for_state(state: dict[str, Any]) -> str:
    return state_filename(
        state["checkpoint"]
    )


# ---------------------------------------------------------------------------
# Archive verification
# ---------------------------------------------------------------------------

def verify_archive(
    drive,
    archive_id: str,
) -> None:

    archive_folder = find_backup_folder(
        drive,
        archive_id,
    )

    manifest = load_archive_manifest(
        drive,
        archive_folder,
    )

    print(f"Archive       : {manifest['archive_id']}")
    print(f"Repository    : {manifest['repository_name']}")
    print(f"Created       : {manifest['created_at']}")

    chain = validate_checkpoint_chain(
        drive,
        archive_folder,
    )

    if not chain:
        print("Archive contains no checkpoints.")
        return

    print(f"Checkpoints   : {len(chain)}")

    for index, (state_file, state) in enumerate(chain, start=1):

        bundle_name = state["bundle"]["filename"]

        bundle_file = find_unique_file(
            drive,
            parent_id=archive_folder["id"],
            name=bundle_name,
        )

        if not bundle_file:
            raise RuntimeError(
                f"Checkpoint {state['checkpoint']} is missing "
                f"bundle {bundle_name}."
            )

        print(
            f"[{index}/{len(chain)}] "
            f"Verifying {bundle_name}..."
        )

        bundle_data = download_drive_file(
            drive,
            bundle_file["id"],
        )

        actual_sha = sha256_bytes(bundle_data)
        expected_sha = state["bundle"]["sha256"]

        if actual_sha != expected_sha:
            raise RuntimeError(
                f"SHA-256 mismatch for {bundle_name}:\n"
                f"Expected: {expected_sha}\n"
                f"Actual:   {actual_sha}"
            )

        expected_size = state["bundle"]["size"]

        if len(bundle_data) != expected_size:
            raise RuntimeError(
                f"Size mismatch for {bundle_name}."
            )

        with tempfile.TemporaryDirectory() as temp:
            bundle_path = Path(temp) / bundle_name
            bundle_path.write_bytes(bundle_data)

            verify_bundle(bundle_path)

    print()
    print("Archive verification successful.")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def initialize_repository(destination: Path) -> Path:
    destination = destination.resolve()

    destination.mkdir(
        parents=True,
        exist_ok=True,
    )

    if any(destination.iterdir()):
        raise RuntimeError(
            f"Restore destination is not empty:\n{destination}\n\n"
            f"Restore intentionally refuses to overwrite existing files."
        )

    run_command(
        ["git", "init", "--quiet", str(destination)]
    )

    return destination / ".git"


def import_bundle(
    git_dir: Path,
    bundle_path: Path,
) -> None:

    # Fetch the bundle into the repository. The refs in the bundle are
    # merely transport mechanisms. The authoritative refs are restored
    # explicitly from the checkpoint state afterward.
    run_command(
        [
            "git",
            f"--git-dir={git_dir}",
            "fetch",
            "--quiet",
            str(bundle_path),
            "refs/*:refs/*",
        ]
    )


def set_restored_refs(
    git_dir: Path,
    refs: dict[str, str],
) -> None:

    # First remove refs that git init/fetch may have created.
    existing = get_all_refs(git_dir)

    for ref in existing:
        if ref not in refs:
            run_git(
                git_dir,
                ["update-ref", "-d", ref],
            )

    for ref, sha in refs.items():
        run_git(
            git_dir,
            ["update-ref", ref, sha],
        )


def set_restored_head(
    git_dir: Path,
    head: dict[str, str],
) -> None:

    if head["type"] == "symbolic":
        run_git(
            git_dir,
            ["symbolic-ref", "HEAD", head["ref"]],
        )

    elif head["type"] == "detached":
        run_git(
            git_dir,
            ["update-ref", "HEAD", head["sha"]],
        )

    else:
        raise RuntimeError(
            f"Unknown HEAD type: {head['type']}"
        )


def verify_restored_repository(
    git_dir: Path,
    expected_state: dict[str, Any],
) -> None:

    print("Running git fsck...")

    run_git(
        git_dir,
        ["fsck", "--full"],
    )

    actual_refs = get_all_refs(git_dir)

    if actual_refs != expected_state["refs"]:
        raise RuntimeError(
            "Restored refs do not match checkpoint."
        )

    actual_head = get_head(git_dir)

    if actual_head != expected_state["head"]:
        raise RuntimeError(
            "Restored HEAD does not match checkpoint."
        )

    print("Restored repository verified.")


def restore_archive(
    drive,
    archive_id: str,
    destination: Path,
    checkpoint_name_arg: str | None = None,
) -> None:

    archive_folder = find_backup_folder(
        drive,
        archive_id,
    )

    manifest = load_archive_manifest(
        drive,
        archive_folder,
    )

    chain = validate_checkpoint_chain(
        drive,
        archive_folder,
    )

    if not chain:
        raise RuntimeError(
            "Archive contains no checkpoints."
        )

    selected_index = len(chain) - 1

    if checkpoint_name_arg:

        checkpoint_state_name = (
            checkpoint_name_arg
            if checkpoint_name_arg.endswith(STATE_SUFFIX)
            else state_filename(checkpoint_name_arg)
        )

        matches = [
            index
            for index, (state_file, _) in enumerate(chain)
            if state_file["name"] == checkpoint_state_name
        ]

        if len(matches) != 1:
            available = "\n".join(
                f"  {state_file['name']}"
                for state_file, _ in chain
            )

            raise RuntimeError(
                f"Checkpoint not found: {checkpoint_name_arg}\n\n"
                f"Available checkpoints:\n{available}"
            )

        selected_index = matches[0]

    selected_chain = chain[:selected_index + 1]

    selected_state = selected_chain[-1][1]

    print(f"Archive    : {manifest['archive_id']}")
    print(f"Repository : {manifest['repository_name']}")
    print(f"Restoring  : {selected_state['checkpoint']}")
    print(f"Destination: {destination.resolve()}")
    print()

    git_dir = initialize_repository(destination)

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)

        for index, (state_file, state) in enumerate(
            selected_chain,
            start=1,
        ):

            bundle_name = state["bundle"]["filename"]

            bundle_file = find_unique_file(
                drive,
                parent_id=archive_folder["id"],
                name=bundle_name,
            )

            if not bundle_file:
                raise RuntimeError(
                    f"Missing bundle: {bundle_name}"
                )

            print(
                f"[{index}/{len(selected_chain)}] "
                f"Downloading {bundle_name}..."
            )

            bundle_data = download_drive_file(
                drive,
                bundle_file["id"],
            )

            actual_sha = sha256_bytes(bundle_data)

            if actual_sha != state["bundle"]["sha256"]:
                raise RuntimeError(
                    f"SHA-256 mismatch for {bundle_name}."
                )

            if len(bundle_data) != state["bundle"]["size"]:
                raise RuntimeError(
                    f"Size mismatch for {bundle_name}."
                )

            bundle_path = temp_dir / bundle_name
            bundle_path.write_bytes(bundle_data)

            verify_bundle(bundle_path)

            print("Importing bundle...")

            import_bundle(
                git_dir,
                bundle_path,
            )

        print("Restoring refs...")
        set_restored_refs(
            git_dir,
            selected_state["refs"],
        )

        print("Restoring HEAD...")
        set_restored_head(
            git_dir,
            selected_state["head"],
        )

        verify_restored_repository(
            git_dir,
            selected_state,
        )

    print()
    print("Restore complete.")
    print(f"Repository restored to: {destination.resolve()}")


# ---------------------------------------------------------------------------
# Archive listing
# ---------------------------------------------------------------------------

def list_archives(drive) -> None:

    root_files = list_drive_files(
        drive,
        name=DRIVE_ROOT_NAME,
        mime_type="application/vnd.google-apps.folder",
    )

    if not root_files:
        print("No Git Backups archive exists.")
        return

    if len(root_files) > 1:
        raise RuntimeError(
            f"Multiple '{DRIVE_ROOT_NAME}' folders exist."
        )

    folders = list_drive_files(
        drive,
        parent_id=root_files[0]["id"],
        mime_type="application/vnd.google-apps.folder",
    )

    if not folders:
        print("No archives found.")
        return

    for folder in sorted(
        folders,
        key=lambda x: x["name"],
    ):
        try:
            manifest = load_archive_manifest(
                drive,
                folder,
            )

            print(
                f"{manifest['archive_id']}"
                f"\t{manifest['repository_name']}"
            )

        except RuntimeError:
            print(
                f"{folder['name']}"
                f"\t[INVALID ARCHIVE]"
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "Append-only incremental Git backup to Google Drive."
        )
    )

    parser.add_argument(
        "path",
        nargs="?",
        help=(
            "Git directory for backup, or empty destination directory "
            "for restore."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create and verify the bundle but do not upload it.",
    )

    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the archive associated with the supplied Git directory.",
    )

    parser.add_argument(
        "--verify-archive",
        metavar="ARCHIVE_ID",
        help="Verify a Drive archive without the original repository.",
    )

    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore an archive into an empty directory.",
    )

    parser.add_argument(
        "--archive",
        metavar="ARCHIVE_ID",
        help="Archive ID used for restore.",
    )

    parser.add_argument(
        "--checkpoint",
        metavar="CHECKPOINT",
        help="Checkpoint name or .state.json filename to restore.",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_archives",
        help="List available Drive archives.",
    )

    return parser.parse_args()


def main() -> int:

    args = parse_args()

    try:

        if args.list_archives:

            drive = get_drive()
            list_archives(drive)
            return 0

        if args.verify_archive:

            drive = get_drive()

            verify_archive(
                drive,
                args.verify_archive,
            )

            return 0

        if args.restore:

            if not args.archive:
                raise RuntimeError(
                    "--restore requires --archive ARCHIVE_ID"
                )

            if not args.path:
                raise RuntimeError(
                    "--restore requires an empty destination directory."
                )

            if args.dry_run:
                raise RuntimeError(
                    "--dry-run cannot be combined with --restore."
                )

            drive = get_drive()

            restore_archive(
                drive,
                args.archive,
                Path(args.path),
                args.checkpoint,
            )

            return 0

        if args.verify:

            if not args.path:
                raise RuntimeError(
                    "--verify requires a Git directory."
                )

            git_dir = Path(args.path).resolve()

            verify_git_repository(git_dir)

            archive_id, _ = repository_identity(
                git_dir
            )

            drive = get_drive()

            verify_archive(
                drive,
                archive_id,
            )

            return 0

        if not args.path:
            raise RuntimeError(
                "A Git directory is required for backup."
            )

        backup(
            Path(args.path),
            dry_run=args.dry_run,
        )

        return 0

    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
