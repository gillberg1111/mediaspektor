# Changelog

All notable changes to **MediaSpektor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to a simple `v0.x` release line.

## [v1.2.9] - 2026-06-14

### Fixed
- **Dashboard "Total space reclaimed" and item count no longer multi-count.** A movie archived across Plex + Jellyfin + Emby has one DB row per server; the stats summed raw rows, so a single title counted 2–3×. Stats now collapse to one row per physical file (`original_path`) before summing — the movie/show grids already de-duped, now the headline numbers match.

### Changed
- Sidebar wordmark now reads **MediaSpektor** ("Media" in white, "Spektor" in ghost-green) to match the login/branding wordmark, instead of just "Spektor".
- Background gradient eased off the all-green look: a faint **spectral indigo** drift pool is mixed in alongside the mint (and a touch of teal), for a more ghostly, less uniformly-green atmosphere.

## [v1.2.8] - 2026-06-14

### Fixed
- **Jellyfin & Emby poster uploads returned HTTP 500.** Their `POST /Items/{id}/Images/Primary` endpoint expects the image body **Base64-encoded** (the `Content-Type` header carries the real MIME type); we were sending raw bytes. Affected both archival and "Fix Poster" regeneration — Plex was unaffected.
- **Activity log showed every line twice.** The in-memory log handler was attached to both the `mediaspektor` logger and the root logger it propagates to, capturing each record twice. Now attached to root only.

### Changed
- Poster overlay now draws a mint **border frame** around the whole poster (not just the banner's top edge), so the archived state reads at a glance.
- The badge shows just **`ARCHIVED`** (no "• 0.0 GB SAVED") when the saved figure is zero/unknown — which is what regenerating a poster produced when a row's stored size was stale.
- "Fix Poster" regeneration re-applies the real saved amount: it now uses the largest saving recorded across all of that title's per-server rows, rather than blindly trusting the clicked server's row.

## [v1.2.7] - 2026-06-14

### Changed
- The dummy placeholder videos (`.mp4`/`.mkv`/`.avi`) are now ~10 seconds long instead of 2 — the previous clip was too short to read the on-screen "This title was archived…" message before playback ended. Same branding/frame, longer hold.

## [v1.2.6] - 2026-06-14

### Security
- Session cookie `Secure` flag is now driven by an explicit `security.https_only` config option instead of the request scheme. Behind a reverse proxy, `X-Forwarded-Proto` is attacker-spoofable, so deriving `Secure` from it could be tricked off — the flag now reflects operator intent only.
- Proxy headers are no longer trusted from every client. `proxy_headers`/`forwarded_allow_ips` are only enabled when `security.trusted_proxies` is set to the proxy's address(es); the previous unconditional `forwarded_allow_ips="*"` let any client spoof its scheme and source address.
- Changing the dashboard password (`/api/change-password`) or credentials via `/api/config` now revokes all other active sessions, keeping only the caller's — a leaked or stale cookie can no longer outlive a credential change.

### Added
- `security.https_only` and `security.trusted_proxies` config options (see `config.yaml.example`).

## [v1.2.5] - 2026-06-14

### Fixed
- **CRITICAL data-loss fix:** The single-item "Confirm Spektor" archive (the dashboard button) ignored the `safety.dry_run` switch entirely — it swapped the real media file for a dummy even with Dry-Run enabled, irreversibly destroying originals when `backup_original_media` was off. The manual archive now honors Dry-Run exactly like the scheduled run: it logs `[DRY-RUN] Would Spektor…` and changes nothing.
- Added a data-safety guard to the "Fix Video" regeneration: it now refuses to run unless the item's status is `archived` **and** the on-disk file is small enough to be a dummy (≤ 50 MB), so it can never overwrite real media (it writes in place with no backup).
- Failure notifications used an unhandled `"danger"` toast type and rendered as a green success checkmark with no error styling — real errors looked like successes. Corrected to the styled `error` type.
- The "Fix Poster" / "Fix Video" buttons used an undefined `btn-info` CSS class and rendered unstyled; restyled with the existing `btn-secondary`.

### Added
- **Pause/Resume control on the dashboard activity log.** The live log refreshes every 2 s, which cleared any in-progress text selection; you can now freeze it to highlight and copy, then resume.

## [v1.2.4] - 2026-06-14

### Fixed
- Fixed backend `AttributeError` crash during the "Fix Poster" procedure caused by hallucinated API methods `get_item_details` and `_find_item_across_servers`. Sibling item identification is now performed using direct database queries.

## [v1.2.3] - 2026-06-14

### Fixed
- Removed a local diagnostic script (`test_regen.py`) that was accidentally committed in `v1.2.2` and was causing the GitHub Actions CI pipeline to crash during the `unittest discover` step.

## [v1.2.2] - 2026-06-14

### Fixed
- Fixed an `AttributeError` backend crash during the `Fix Poster` and `Fix Video` procedures caused by an incorrectly referenced `_get_server` method.
- Added explicit backend logging for regenerate endpoints so background errors are properly visible in the UI dashboard logs.

## [v1.2.1] - 2026-06-14

### Fixed
- Fixed a bug where the `Fix Poster` and `Fix Video` buttons in the confirmation modal were missing their click handlers and doing nothing when clicked.

## [v1.2.0] - 2026-06-13

### Added
- **Regenerate Poster Feature:** Added a `Fix Poster` button to the UI for archived items. This reads the clean original poster backup from disk, re-applies the archived overlay (which fixes the tiny text bug from earlier versions), and re-uploads it natively to Plex, Jellyfin, and Emby without fully restoring the item.
- **Regenerate Video Feature:** Added a `Fix Video` button to the UI for archived items. This rewrites the dummy video and re-applies configured PUID/PGID permissions, providing an easy way to fix broken file ownership manually without a full cycle.

## [v1.1.2] - 2026-06-13

### Fixed
- **Illegible tiny text on poster overlays.** The script previously defaulted to searching the system for `Arial`, which is unavailable on Linux/Docker by default. This caused Pillow to silently fall back to an unscalable 11-pixel default bitmap font, rendering the "ARCHIVED" text as a tiny speck. The default font is now explicitly set to `DejaVuSans.ttf`, which was already included in the `v1.0.0` Docker image.

## [v1.1.1] - 2026-06-13

### Fixed
- **Jellyfin and Emby poster upload failures.** Injected the required `Content-Type: image/jpeg` header into the Jellyfin and Emby poster upload API requests.
- **500 Internal Server Errors from alpha channels.** Jellyfin and Emby were occasionally crashing internally (500 Server Error) when attempting to parse the transparent RGBA PNG overlay generated by Pillow. The poster overlay is now automatically converted to standard RGB and saved as a `JPEG` to guarantee native processing compatibility across all media servers.

## [v1.1.0] - 2026-06-13

### Added
- **Dashboard Security settings section** — enable login and set the username/password directly in Settings (previously only editable in `config.yaml`).
- **First-run forced password change** — when the dashboard is still on the default `admin` password, a non-dismissable prompt requires setting a strong one (min 6 chars) before continuing, via a new `/api/change-password` endpoint.

### Fixed
- **Saving Settings no longer disables auth.** The settings form omitted the `security` block, so saving wiped it (turning authentication off and losing the password). The Security section now includes it, and `/api/config` preserves the existing `security` block if a client omits it.

### Security
- The session cookie is marked **`Secure`** when the dashboard is served over HTTPS, and uvicorn now honors proxy headers (`X-Forwarded-Proto`) so HTTPS is detected behind a reverse proxy.

## [v1.0.3] - 2026-06-13

### Security
- **Removed the over-permissive CORS config** (`allow_origins=["*"]` with `allow_credentials=True`). The dashboard and API are same-origin, so no CORS is needed; the wildcard+credentials combination is both rejected by browsers and a security smell.
- **Constant-time login comparison** — credentials are now checked with `hmac.compare_digest` instead of `==`, avoiding timing side channels.
- **Loud startup warning** when dashboard auth is disabled or still using the default `admin` password, since the dashboard exposes server tokens and can overwrite media.

## [v1.0.2] - 2026-06-13

### Fixed
- **Container crash on startup** when the backup directory was the seeded placeholder (`/path/to/cold/storage/backup`). Running as the unprivileged Unraid user, `mkdir` failed and the fallback (`./backups` under the root-owned `/app`) also failed, so the app never started. The backup directory now treats a blank/placeholder value as "unset" and defaults to a writable `<config dir>/backups` (e.g. `/config/backups`), with the same writable fallback on any error. `config.yaml.example` now ships a blank `backup_directory`.

## [v1.0.1] - 2026-06-13

### Fixed
- **Poster upload race during archival.** `archive_item` now uploads the badged poster to every matched server **before** swapping the file. Swapping first tripped the server's inotify rescan, which invalidated the item record mid-upload and caused 404/400/500 errors. On a file-swap failure, posters are rolled back to the originals.
- **Dummy file permissions.** The replacement dummy now inherits the original file's permissions (and ownership, when privileged) instead of the `0600` that `tempfile.mkstemp` forces. A media server running as a different user can now read the dummy and rescan it, instead of dropping the item from the library.

### Added
- **Unraid PUID/PGID/UMASK support.** The container starts as root and drops to the configured user (default `99:100` = `nobody:users`) via `gosu`, so files it writes to your media share match the array's usual ownership instead of `root:root`. Exposed in the Unraid template and the `docker run` docs.

## [v1.0] - 2026-06-13

First stable public release — published to the Unraid Community Applications catalog.

### Added
- **MIT License.**

MediaSpektor is now feature-complete for 1.0: multi-server archive propagation across Plex/Jellyfin/Emby with an optional TMDB ID bridge, a single-accent "ghost" brand identity, installable PWA, Dockerized with CI-published GHCR images, atomic data-safe archiving, and a mint-accented poster overlay.

## [v0.11] - 2026-06-13

### Changed
- **Poster overlay matches the brand.** The "ARCHIVED • X GB SAVED" badge now uses the mint accent (`#3ECF8E`) for its border and near-black (`#080B0A`) for the banner fill, replacing the old gold/dark-gray scheme. Updated the `PosterOverlay` defaults, `config.yaml.example`, and the Settings color placeholders, and refreshed README wording (dropped the "premium glassmorphic" language).

## [v0.10] - 2026-06-13

### Added
- **PWA / Add to Home Screen** — added a web manifest, brand icon set (180×180 apple-touch-icon, 192/512 maskable PNGs, SVG favicon), and the iOS/Android meta tags, so MediaSpektor installs with the correct ghost icon on iPhone and Android.

### Changed
- **Rebranded the dummy replacement videos.** The `.mp4`/`.mkv`/`.avi` stubs that replace archived media now show the current identity — the mint ghost glyph + Media·Spektor wordmark on near-black — instead of the old purple ghost. Regenerated by `scratch/generate_dummy_videos.py` (and much smaller: the `.avi` dropped from ~137 KB to ~15 KB).

### Removed
- Deleted the unused legacy logo assets (`static/logo.svg`, `logo_32/64/128/512.png`) and the old asset/build scripts now that the brand assets and a single generator replace them. Build artifacts under `scratch/videos/` are git-ignored.

## [v0.9] - 2026-06-13

### Added
- **Docker image + CI** — a `Dockerfile`, `requirements.txt`, and a GitHub Action that tests, builds, and publishes a multi-arch (amd64/arm64) image to GHCR on every push to `main` and every `vX.Y` tag. Intended to run on Unraid alongside Plex/Jellyfin/Emby with the media share mounted at the same `/data` path. README gains a **Deploy on Unraid (Docker)** section.
- **Brand system** — adopted the MediaSpektor style guide: new ghost logo assets (sidebar icon + SVG favicon), the `Media`(white) + `Spektor`(ghost-green) wordmark, and the "ghost" highlight rule in brand copy.

### Changed
- **Full UI redesign.** Reworked layout (hero-led dashboard, cinematic poster-forward media cards, refined sidebar/header), single mint accent (`#3ECF8E`) on near-black per the style guide, Space Grotesk + JetBrains Mono typography, ambient background glow with subtle drift, and tasteful frosted-glass surfaces. New tagline: "Reclaim your space. Keep your library. *A ghost of what you watched.*"

### Fixed
- **Data-safety on archive.** Archiving now verifies the original file exists and its directory is writable **before** any deletion, and swaps in the dummy via an atomic temp-file replace — so a bad path or permission error (e.g. an unmounted `/data`) can never destroy the original. Clear, actionable error messages replace the raw `Permission denied`.

## [v0.8] - 2026-06-13

### Fixed
- **Spektor/Restore 422** — `/api/spektor` and `/api/restore` no longer reject Plex items. `ActionReq.item_id` now accepts numeric ids (Plex ratingKeys arrive as JSON numbers) and coerces them to strings.
- **Duplicate movies/shows** — when multiple servers point at one shared library, `/api/movies` and `/api/shows` now dedupe across servers (movies by file path → title+year, shows by title+year), keeping one card and preferring an already-archived one.
- **Emby showed nothing** — `EmbyConnector` now resolves the configured `user_id` (a username *or* a GUID) to the real user GUID via Emby's `/Users` list at startup, so user-scoped queries stop 500-ing.
- **Library-name matching** — Jellyfin/Emby library lookups are now case-insensitive and log the available library names on a miss, so a configured name that doesn't match is never silent.
- **Search box stuck after tab switch** — switching tabs now clears the Movies/TV Shows search fields, so returning to a tab shows the full list with an empty, editable search box (the Refresh button still preserves an active query).

### Changed
- Corrected the default Target Libraries per server to match each platform's out-of-the-box naming: Plex `Movies, TV Shows`, Jellyfin `Movies, Shows`, Emby `Movies, TV Shows` (placeholders and `config.yaml.example`).

## [v0.7] - 2026-06-12

### Added
- **Multi-server propagation** — archiving (and restoring) an item now propagates to **every enabled server** that shares the same physical media. The file on disk is replaced once; the "ARCHIVED" poster overlay and archived state are then pushed to Plex, Jellyfin, and Emby. One database row is recorded per server so the status badge reflects archived/restored everywhere in the UI.
- **Cross-server item matching** — a new `find_item` matcher on every connector resolves the same title across servers using **file path first**, then external IDs (TMDB → IMDB → TVDB) for movies. External IDs (`tmdb`/`imdb`/`tvdb`) are now extracted from Plex GUIDs and Jellyfin/Emby `ProviderIds`.
- **TMDB ID bridge** — an optional, key-gated `TmdbClient` normalizes a movie's IDs across systems (so a Plex item with only an IMDB id still matches a Jellyfin item with only a TMDB id). Configure under `integrations.tmdb.api_key` (v3 key or v4 bearer token) or the `TMDB_API_KEY` env var; without a key, matching gracefully falls back to path + direct ID overlap. Added a TMDB API-key field to the Integrations settings UI.

### Changed
- `restore` now fans out across all sibling rows for a physical item (restores the file once, restores each server's poster, and updates status on every row).
- Per-server poster propagation is **best-effort**: a server with no confident match is skipped with a warning rather than failing the whole operation.

### Notes
- Episode matching is **file-path only** for now (intentional — the shared library is mounted at the same path on every server). ID-based episode matching is deferred.

## [v0.6] - 2026-06-12

### Fixed
- **Plex data fetching with string IDs** — `PlexConnector` now casts all-digit string `ratingKey`s to `int` via a `_resolve_id` helper before calling `fetchItem`, across `download_poster`, `upload_poster`, `get_seasons`, `get_episodes`, and `get_item_metadata`. The `/api/posterproxy` route applies the same cast. This resolves Plex retrieval errors caused by string-based IDs.

### Added
- **Poster HTTP caching** — `/api/posterproxy` now returns a `Cache-Control: public, max-age=86400` header for Plex, Jellyfin, and Emby posters so browsers cache proxied artwork.
- **Frontend in-memory list caching** — Movies and TV Shows lists are cached in JS (`currentMovies` / `currentShows`). Swapping tabs renders instantly from cache instead of re-fetching and showing a loading spinner. `loadMovies(force)` / `loadShows(force)` accept a force flag to bypass the cache.
- **Status-based poster cache-busting** — Movie and show poster URLs include a `&status=` query param so the browser only re-fetches a poster when an item's archived/restored status changes (e.g. after a glassmorphic overlay is applied).
- **Test coverage** — Added `test_poster_proxy` validating Plex string→int ID conversion, the `200` response, and the `Cache-Control` header.

### Changed
- Cache is force-refreshed (bypassed) on the manual **Refresh** button, after a **Spektor/Restore** action completes, and both lists are cleared when **settings are saved successfully**, so updated library status is immediately visible.

## [v0.5]

### Added
- **Login security** with username/password authentication and session-cookie gating of API endpoints (toggled via `security.enabled`).
- **Username/password-based Jellyfin authentication** (in addition to API key flows).
- **Custom dummy videos** and a **premium logo** asset set.
- **`safety.allow_automated_archival` toggle** — automated `--archive` runs are forced into dry-run unless this is explicitly enabled, with a warning and a confirmation popup in the settings UI.
- **Structured settings UI** — per-section page version footers and a floating save button.

### Fixed
- Aligned `logo.svg` geometry with the circular-feet retro ghost shape used in `logo.png`.
- Fixed search icon overlap in the media grids.

## [v0.4]

### Fixed
- Restored a missing closing brace for the confirmation modal click listener in `app.js`.

## [v0.3]

### Changed
- Replaced the raw JSON config editor with a structured HTML settings form.

## [v0.2]

### Changed
- Linked `app.js` to the HTML and elevated the design system with a premium glassmorphic theme.

## [v0.1]

### Added
- Initial release: MediaSpektor self-hosted watch-state storage archiver dashboard, with Plex/Jellyfin/Emby connectors, SQLite state tracking, Pillow poster overlays, Radarr/Sonarr integration, dummy-video generation, and a FastAPI web dashboard.
