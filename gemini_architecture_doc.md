# MediaSpektor Architecture Document: Bug Fixes for v1.1

## Overview
This document outlines the root causes and proposed architectural fixes for two distinct bugs currently affecting the MediaSpektor application during the archival (`Spektoring`) process:
1. **Poster Upload Failures**: HTTP 404/400/500 errors occurring on Plex, Jellyfin, and Emby when attempting to upload the "ARCHIVED" poster overlay.
2. **File Permission Denial**: Plex (and other servers) failing to rescan the dummy file because it is created with restrictive `0600` permissions.

This document is designed to be handed off to an engineer or AI assistant (e.g., Claude) to cleanly implement the fixes.

---

## 1. Poster Upload Race Condition (HTTP Failures)

### The Problem
During single-item archival (`archive_item` in `mediaspektor.py`), users observe success logs for the file swap but immediate HTTP 500, 400, or 404 errors when the script attempts to upload the poster overlay to Plex, Jellyfin, and Emby. 

### Root Cause
The operations in `archive_item` are currently executed in this order:
1. Swap the physical media file on disk (`self._replace_with_dummy`).
2. Loop over all connected media servers to download, overlay, and upload the new posters.

Media servers utilize real-time directory monitoring (e.g., `inotify`). The moment step 1 swaps the file, the media servers instantly detect the modification and begin a background rescan. This rescan immediately locks, invalidates, or completely deletes the database record for that media item. When step 2 attempts to upload a poster to the server a few seconds later using the original `item_id`, the API rejects the request because the record is either locked or missing.

### Proposed Fix
The order of operations inside `archive_item` must be inverted to guarantee the media servers are entirely stable during the poster upload.
1. **Iterate Servers First**: Loop through all connected media servers. For each, download the poster, apply the overlay, and upload the new poster via API. Store rollback data in memory (e.g., the backup poster path) in case the file swap fails later.
2. **Swap File**: Call `self._replace_with_dummy` to swap the physical file on disk. If this fails, catch the exception and use the rollback data to upload the original posters back to the servers.
3. **Trigger Scans**: Call `server.trigger_library_scan()` on all servers to alert them to the new dummy file.

---

## 2. Restrictive Dummy File Permissions

### The Problem
After archival, the media server drops the media item entirely instead of rescanning it. Upon inspection, the newly created dummy video file has incorrect and highly restrictive permissions: `-rw------- 1 root root` (`0600`).

### Root Cause
In `self._replace_with_dummy()`, the script uses Python's `tempfile.mkstemp` to safely write the dummy bytes to disk before performing an atomic `os.replace`. 
By design, `mkstemp` forces strict `0600` permissions on newly created temporary files for security. When `os.replace` swaps the file, it preserves these restrictive permissions. 
Even with the newly implemented Unraid `PUID/PGID` support (which runs the process as `nobody:users`), the file will still be written as `0600 nobody:users`. If a media server container (like Plex) runs as a slightly different user (e.g., `plex`), it will receive an "Access Denied" error when attempting to read the file, causing it to drop the item from the library.

### Proposed Fix
Modify `_replace_with_dummy` to intelligently clone the permissions and ownership of the original media file and apply them to the dummy file.

1. **Capture Stats**: Before replacing the file, capture the original file's metadata: `orig_stat = os.stat(file_path)`.
2. **Apply Permissions**: After creating the `mkstemp` file, explicitly copy the original permissions using `os.chmod(tmp_path, orig_stat.st_mode)`. This ensures permissions like `0644` or `0666` are perfectly preserved.
3. **Apply Ownership (Best Effort)**: Attempt to copy the original ownership using `os.chown(tmp_path, orig_stat.st_uid, orig_stat.st_gid)`. 
   - **Crucial Note**: Wrap `os.chown` in a `try...except OSError:` block. If the container is running under `PUID/PGID` as an unprivileged user (`nobody`), the script will lack the system privileges to change ownership, which will raise an `OSError`. Catching and ignoring this error is the desired behavior—the file will simply remain owned by the unprivileged user but will still possess the open `chmod` permissions applied in step 2, ensuring the media server can read it.
