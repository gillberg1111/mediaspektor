# Changelog

All notable changes to **MediaSpektor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to a simple `v0.x` release line.

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
