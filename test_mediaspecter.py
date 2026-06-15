#!/usr/bin/env python3
"""Unit tests for MediaSpecter."""

import unittest
from unittest.mock import MagicMock, patch
import os
import tempfile
import sqlite3
import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
from PIL import Image

# Import the code to test
from mediaspecter import (
    _parse_iso_date,
    Database,
    PosterOverlay,
    MediaSpecter,
    RadarrClient,
    SonarrClient
)


class TestHelpers(unittest.TestCase):
    def test_parse_iso_date(self):
        # Normal ISO with decimal seconds and Z
        dt = _parse_iso_date("2026-06-12T21:08:39.1234567Z")
        self.assertEqual(dt, datetime(2026, 6, 12, 21, 8, 39))

        # ISO with Z but no decimals
        dt = _parse_iso_date("2026-06-12T21:08:39Z")
        self.assertEqual(dt, datetime(2026, 6, 12, 21, 8, 39))

        # None / invalid inputs
        self.assertIsNone(_parse_iso_date(None))
        self.assertIsNone(_parse_iso_date("invalid-date"))


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db = Database(self.temp_db.name)

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)

    def test_crud_operations(self):
        # Insert
        item = {
            "server_type": "plex",
            "server_item_id": "12345",
            "title": "Test Movie",
            "media_type": "movie",
            "original_path": "/path/to/movie.mkv",
            "original_size_bytes": 10 * 1024 * 1024 * 1024, # 10 GB
            "dummy_size_bytes": 2048,
            "backup_poster_path": "/backup/12345.jpg",
            "backup_media_path": None,
            "status": "archived"
        }
        self.db.insert(**item)

        # Check existence
        self.assertTrue(self.db.item_exists("plex", "12345"))
        self.assertFalse(self.db.item_exists("plex", "99999"))

        # Get item
        fetched = self.db.get_item("plex", "12345")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["title"], "Test Movie")
        self.assertEqual(fetched["status"], "archived")

        # Update status
        self.db.update_status("plex", "12345", "restored")
        fetched = self.db.get_item("plex", "12345")
        self.assertEqual(fetched["status"], "restored")

        # Stats (since status is restored, stats should show 0 archived)
        stats = self.db.get_stats()
        self.assertEqual(stats["total_items"], 0)

        # Change status back to archived and verify stats
        self.db.update_status("plex", "12345", "archived")
        stats = self.db.get_stats()
        self.assertEqual(stats["total_items"], 1)
        self.assertEqual(stats["total_original_bytes"], 10 * 1024 * 1024 * 1024)
        self.assertGreater(stats["total_saved_bytes"], 0)


    def test_stats_dedupe_same_file_across_servers(self):
        # Same physical movie archived on three servers — one row each.
        for srv, sid in [("plex", "1"), ("jellyfin", "2"), ("emby", "3")]:
            self.db.insert(
                server_type=srv, server_item_id=sid, title="Movie", media_type="movie",
                original_path="/data/Movie.mp4",
                original_size_bytes=10 * 1024 * 1024 * 1024, dummy_size_bytes=2048,
                backup_poster_path=None, backup_media_path=None, status="archived",
            )
        stats = self.db.get_stats()
        # Counted once, saved space counted once — not tripled.
        self.assertEqual(stats["total_items"], 1)
        self.assertEqual(stats["total_original_bytes"], 10 * 1024 * 1024 * 1024)


class TestPosterOverlay(unittest.TestCase):
    def setUp(self):
        self.config = {
            "aesthetics": {
                "enable_poster_overlay": True,
                "banner_color": [8, 11, 10, 204],
                "border_color": [62, 207, 142, 255],
                "font_name": "Arial",
                "font_size_ratio": 0.05
            }
        }
        self.overlay = PosterOverlay(self.config)

    def test_apply_overlay(self):
        # Create a tiny dummy poster image
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_in:
            img = Image.new("RGB", (200, 300), (255, 255, 255))
            img.save(tmp_in.name, "JPEG")
            input_path = tmp_in.name

        output_path = input_path + ".overlay.png"

        try:
            # Apply
            success = self.overlay.apply_overlay(input_path, output_path, gb_saved=4.5)
            self.assertTrue(success)
            self.assertTrue(os.path.exists(output_path))

            # Verify dimensions
            with Image.open(output_path) as out_img:
                self.assertEqual(out_img.size, (200, 300))
                self.assertEqual(out_img.format, "JPEG")
        finally:
            if os.path.exists(input_path):
                os.unlink(input_path)
            if os.path.exists(output_path):
                os.unlink(output_path)


class TestOrchestratorFilter(unittest.TestCase):
    @patch("mediaspecter.Database")
    def setUp(self, mock_db):
        # Setup mock config
        self.config = {
            "servers": [],
            "rules": {
                "min_age_days": 7,
                "exclude_labels": ["keep", "preserved"],
                "exclude_genres": ["Documentary", "Special"],
                "dummy_threshold_mb": 15
            },
            "safety": {
                "dry_run": True,
                "backup_original_media": False
            }
        }
        
        # Write temporary config
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config, f)

        self.specter = MediaSpecter(self.temp_config.name)

    def tearDown(self):
        if os.path.exists(self.temp_config.name):
            os.unlink(self.temp_config.name)

    @patch("os.path.exists")
    def test_filter_items(self, mock_exists):
        mock_exists.return_value = True

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        items = [
            # 1. OK candidate
            {
                "file_path": "/media/ok_movie.mkv",
                "original_size": 100 * 1024 * 1024, # 100 MB
                "last_watched": now - timedelta(days=10),
                "genres": ["Action", "Sci-Fi"],
                "labels": ["public"],
                "title": "OK Movie"
            },
            # 2. Too small
            {
                "file_path": "/media/small.mkv",
                "original_size": 5 * 1024 * 1024, # 5 MB (threshold is 15MB)
                "last_watched": now - timedelta(days=10),
                "genres": ["Comedy"],
                "labels": [],
                "title": "Small Movie"
            },
            # 3. Excluded Label
            {
                "file_path": "/media/keep_me.mkv",
                "original_size": 100 * 1024 * 1024,
                "last_watched": now - timedelta(days=10),
                "genres": ["Drama"],
                "labels": ["keep"],
                "title": "Keep Me"
            },
            # 4. Excluded Genre
            {
                "file_path": "/media/doc.mkv",
                "original_size": 100 * 1024 * 1024,
                "last_watched": now - timedelta(days=10),
                "genres": ["documentary"],
                "labels": [],
                "title": "Documentary"
            },
            # 5. Too recent
            {
                "file_path": "/media/recent.mkv",
                "original_size": 100 * 1024 * 1024,
                "last_watched": now - timedelta(days=2), # watched 2 days ago
                "genres": ["Thriller"],
                "labels": [],
                "title": "Recent Movie"
            },
            # 6. Unknown watch date (should skip for safety when min_age_days > 0)
            {
                "file_path": "/media/unknown_date.mkv",
                "original_size": 100 * 1024 * 1024,
                "last_watched": None,
                "genres": ["Thriller"],
                "labels": [],
                "title": "Unknown Date Movie"
            }
        ]

        filtered = self.specter._filter_items(items)
        
        # Only the first item should pass all rules
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "OK Movie")


class TestOrchestratorArchiveSafety(unittest.TestCase):
    def setUp(self):
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()

    def tearDown(self):
        if os.path.exists(self.temp_config.name):
            os.unlink(self.temp_config.name)

    @patch("mediaspecter.Database")
    def test_archive_downgrades_to_dry_run_when_allow_auto_is_false(self, mock_db):
        config_data = {
            "servers": [],
            "rules": {},
            "safety": {
                "dry_run": False,
                "allow_automated_archival": False
            }
        }
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(config_data, f)
            
        specter = MediaSpecter(self.temp_config.name)
        specter.db.item_exists.return_value = False
        
        mock_server = MagicMock()
        mock_server.server_type = "plex"
        mock_server.config = {"url": "http://mock-plex", "libraries": ["Movies"]}
        mock_server.get_watched_items.return_value = [{
            "id": "1",
            "title": "Test Movie",
            "file_path": "/path/movie.mp4",
            "original_size": 100 * 1024 * 1024,
            "type": "movie",
            "last_watched": datetime.now() - timedelta(days=10),
            "genres": [],
            "labels": []
        }]
        
        specter.servers = [mock_server]
        
        with patch("os.path.exists", return_value=True), \
             patch("os.path.splitext", return_value=("/path/movie", ".mp4")):
            results = specter.archive(dry_run=False)
            self.assertIn("Test Movie", results["archived"])
            self.assertEqual(mock_server.download_poster.call_count, 0)


class TestIntegrations(unittest.TestCase):
    @patch("requests.get")
    @patch("requests.put")
    def test_radarr_unmonitor(self, mock_put, mock_get):
        # Setup mocks
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [
            {
                "id": 42,
                "title": "Target Movie",
                "path": "/media/movies/Target Movie (2025)",
                "folderName": "/media/movies/Target Movie (2025)",
                "monitored": True
            },
            {
                "id": 99,
                "title": "Other Movie",
                "path": "/media/movies/Other Movie (2024)",
                "folderName": "/media/movies/Other Movie (2024)",
                "monitored": True
            }
        ]
        mock_put.return_value.status_code = 200

        client = RadarrClient({"url": "http://mock-radarr:7878", "api_key": "mockkey"})
        
        # Test matching path
        success = client.unmonitor_movie_by_path("/media/movies/Target Movie (2025)/movie.mkv")
        self.assertTrue(success)
        
        # Verify PUT was called with monitored = False
        mock_put.assert_called_once()
        sent_data = mock_put.call_args[1]["json"]
        self.assertFalse(sent_data["monitored"])
        self.assertEqual(sent_data["id"], 42)

    @patch("requests.get")
    @patch("requests.put")
    def test_sonarr_unmonitor(self, mock_put, mock_get):
        # Mock requests behavior
        def mock_get_routing(url, *args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            
            if "api/v3/series" in url:
                mock_resp.json.return_value = [
                    {"id": 1, "path": "/media/tv/Test Show"}
                ]
            elif "api/v3/episodefile" in url:
                mock_resp.json.return_value = [
                    {"id": 99, "path": "/media/tv/Test Show/Season 1/Episode 1.mkv"}
                ]
            elif "api/v3/episode" in url:
                mock_resp.json.return_value = [
                    {"id": 100, "episodeFileId": 99, "monitored": True}
                ]
            return mock_resp

        mock_get.side_effect = mock_get_routing
        mock_put.return_value.status_code = 200

        client = SonarrClient({"url": "http://mock-sonarr:8989", "api_key": "mockkey"})
        
        # Test matching path
        success = client.unmonitor_episode_by_path("/media/tv/Test Show/Season 1/Episode 1.mkv")
        self.assertTrue(success)
        
        # Verify PUT was called on episode 100 to unmonitor it
        mock_put.assert_called_once()
        self.assertTrue("api/v3/episode/100" in mock_put.call_args[0][0])
        self.assertFalse(mock_put.call_args[1]["json"]["monitored"])

    @patch("requests.get")
    @patch("requests.put")
    def test_sonarr_unmonitor_differing_root(self, mock_put, mock_get):
        # Sonarr mounts /tv/...; the media server reports /data/tv/... — match by
        # series folder leaf + episode file basename.
        def routing(url, *args, **kwargs):
            r = MagicMock(); r.status_code = 200
            if "api/v3/series" in url:
                r.json.return_value = [{"id": 1, "path": "/tv/Test Show", "tvdbId": 555}]
            elif "api/v3/episodefile" in url:
                r.json.return_value = [{"id": 99, "path": "/tv/Test Show/Season 1/Episode 1.mkv"}]
            elif "api/v3/episode" in url:
                r.json.return_value = [{"id": 100, "episodeFileId": 99, "monitored": True}]
            return r
        mock_get.side_effect = routing
        mock_put.return_value.status_code = 200

        client = SonarrClient({"url": "http://mock-sonarr:8989", "api_key": "mockkey"})
        success = client.unmonitor_episode_by_path("/data/tv/Test Show/Season 1/Episode 1.mkv")
        self.assertTrue(success)
        self.assertFalse(mock_put.call_args[1]["json"]["monitored"])


class TestFastAPI(unittest.TestCase):
    def setUp(self):
        # We can construct a mock MediaSpecter or a test MediaSpecter
        # with a temp config and database.
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        
        self.config_data = {
            "servers": [
                {
                    "type": "plex",
                    "enabled": False,
                    "url": "http://mock-plex",
                    "token": "mock-token",
                    "libraries": ["Movies"]
                }
            ],
            "rules": {
                "min_age_days": 7,
                "exclude_labels": [],
                "exclude_genres": [],
                "dummy_threshold_mb": 15
            },
            "safety": {
                "dry_run": True,
                "backup_original_media": False,
                "backup_directory": "./backups"
            }
        }
        
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config_data, f)
            
        # Temporarily mock CONFIG_PATH and GLOBAL_SPECTER
        import mediaspecter
        self.old_config_path = mediaspecter.CONFIG_PATH
        self.old_global_specter = mediaspecter.GLOBAL_SPECTER
        mediaspecter.CONFIG_PATH = self.temp_config.name
        
        # Patch the connectors so we don't try connecting to live server
        # when initializing MediaSpecter
        self.mock_connector = MagicMock()
        self.mock_connector.server_type = "plex"
        self.mock_connector.get_movies.return_value = [
            {
                "id": "1",
                "title": "Test Movie 1",
                "original_size": 1024 * 1024 * 100,
                "file_path": "/path/to/movie1.mp4",
                "year": 2025
            }
        ]
        self.mock_connector.get_shows.return_value = [
            {
                "id": "2",
                "title": "Test Show 1",
                "year": 2024
            }
        ]
        self.mock_connector.get_seasons.return_value = [
            {
                "id": "20",
                "title": "Season 1"
            }
        ]
        self.mock_connector.get_episodes.return_value = [
            {
                "id": "201",
                "title": "Episode 1",
                "episode_number": 1,
                "original_size": 1024 * 1024 * 50,
                "file_path": "/path/to/episode1.mp4"
            }
        ]
        
        # Instantiate MediaSpecter and override components
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)
        self.specter.servers = [self.mock_connector]
        
        mediaspecter.GLOBAL_SPECTER = self.specter
        from fastapi.testclient import TestClient
        from mediaspecter import app
        self.client = TestClient(app)

    def tearDown(self):
        import mediaspecter
        mediaspecter.CONFIG_PATH = self.old_config_path
        mediaspecter.GLOBAL_SPECTER = self.old_global_specter
        
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        if os.path.exists(self.temp_config.name):
            os.unlink(self.temp_config.name)

    def test_read_root(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("MediaSpecter", resp.text)

    def test_get_config(self):
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["servers"][0]["type"], "plex")

    def test_update_config(self):
        new_config = dict(self.config_data)
        new_config["rules"]["min_age_days"] = 14
        
        resp = self.client.post("/api/config", json={"config": new_config})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True})
        
        import mediaspecter
        # Verify it reloaded on GLOBAL_SPECTER
        self.assertEqual(mediaspecter.GLOBAL_SPECTER.config["rules"]["min_age_days"], 14)

    def test_get_stats(self):
        resp = self.client.get("/api/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("total_saved_bytes", resp.json())

    def test_get_logs(self):
        resp = self.client.get("/api/logs")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)

    def test_get_movies(self):
        resp = self.client.get("/api/movies")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Test Movie 1")
        self.assertEqual(data[0]["status"], "original")

    def test_get_shows(self):
        resp = self.client.get("/api/shows")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Test Show 1")

    def test_get_seasons(self):
        resp = self.client.get("/api/shows/plex/2/seasons")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Season 1")

    def test_get_episodes(self):
        resp = self.client.get("/api/shows/plex/2/seasons/20/episodes")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Episode 1")
        self.assertEqual(data[0]["status"], "original")

    def test_trigger_actions(self):
        with patch.object(self.specter, "archive_item") as mock_archive:
            resp = self.client.post("/api/specter", json={"server_type": "plex", "item_id": "1"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["success"], True)
            
        with patch.object(self.specter, "restore") as mock_restore:
            resp = self.client.post("/api/restore", json={"server_type": "plex", "item_id": "1"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["success"], True)

    def test_trigger_specter_accepts_numeric_item_id(self):
        # Plex ratingKeys arrive as JSON numbers; must not 422 (regression).
        with patch.object(self.specter, "archive_item") as mock_archive:
            resp = self.client.post("/api/specter", json={"server_type": "plex", "item_id": 12345})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["success"], True)

    def test_poster_proxy(self):
        fake_item = MagicMock()
        fake_item.posterUrl = "http://mock-plex/poster.jpg"
        self.mock_connector._server.fetchItem.return_value = fake_item

        fake_resp = MagicMock()
        fake_resp.content = b"fake-image-data"
        fake_resp.raise_for_status = lambda: None
        fake_resp.headers = {"Content-Type": "image/jpeg"}

        # Poster proxy now uses the pooled HTTP session and reads the body fully.
        with patch("mediaspecter.HTTP.get", return_value=fake_resp):
            resp = self.client.get("/api/posterproxy?server_type=plex&item_id=1")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"fake-image-data")
        self.assertEqual(resp.headers["Cache-Control"], "public, max-age=86400")
        self.mock_connector._server.fetchItem.assert_called_with(1)

    def test_trigger_regenerate_endpoint(self):
        resp = self.client.post("/api/regenerate", json={
            "server_type": "plex",
            "item_id": "1",
            "target": "poster"
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])



class TestAPISecurity(unittest.TestCase):
    def setUp(self):
        import mediaspecter
        self.old_config_path = mediaspecter.CONFIG_PATH
        self.old_global_specter = mediaspecter.GLOBAL_SPECTER
        
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        
        self.config_data = {
            "servers": [],
            "rules": {},
            "safety": {"dry_run": True},
            "security": {
                "enabled": True,
                "username": "testuser",
                "password": "testpassword"
            }
        }
        
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config_data, f)
            
        mediaspecter.CONFIG_PATH = self.temp_config.name
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)
        mediaspecter.GLOBAL_SPECTER = self.specter
        
        from fastapi.testclient import TestClient
        from mediaspecter import app
        self.client = TestClient(app)

    def tearDown(self):
        import mediaspecter
        mediaspecter.CONFIG_PATH = self.old_config_path
        mediaspecter.GLOBAL_SPECTER = self.old_global_specter
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        if os.path.exists(self.temp_config.name):
            os.unlink(self.temp_config.name)

    def test_unauthorized_endpoints(self):
        # Protected endpoints should return 401
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 401)
        
        resp = self.client.get("/api/stats")
        self.assertEqual(resp.status_code, 401)

    def test_login_success_and_access(self):
        # Login with correct credentials
        resp = self.client.post("/api/login", json={
            "username": "testuser",
            "password": "testpassword"
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("success"))
        
        # Session cookie should now be set in the client's cookie jar
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("servers", resp.json())

    def test_login_invalid_credentials(self):
        # Login with wrong password
        resp = self.client.post("/api/login", json={
            "username": "testuser",
            "password": "wrongpassword"
        })
        self.assertEqual(resp.status_code, 401)

    def test_login_flags_default_password(self):
        self.specter.config["security"]["password"] = "admin"
        resp = self.client.post("/api/login", json={"username": "testuser", "password": "admin"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("must_change_password"))

    def test_change_password(self):
        self.client.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        # rejects the default and too-short passwords
        self.assertEqual(self.client.post("/api/change-password", json={"password": "admin"}).status_code, 400)
        self.assertEqual(self.client.post("/api/change-password", json={"password": "abc"}).status_code, 400)
        # accepts a strong one and persists it
        resp = self.client.post("/api/change-password", json={"password": "s3cret-pw"})
        self.assertEqual(resp.status_code, 200)
        import mediaspecter
        self.assertEqual(mediaspecter.GLOBAL_SPECTER.config["security"]["password"], "s3cret-pw")

    def test_change_password_invalidates_other_sessions(self):
        from fastapi.testclient import TestClient
        from mediaspecter import app
        # Two independent logged-in clients (two cookie jars / sessions).
        other = TestClient(app)
        other.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        self.assertEqual(other.get("/api/config").status_code, 200)

        self.client.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        resp = self.client.post("/api/change-password", json={"password": "s3cret-pw"})
        self.assertEqual(resp.status_code, 200)

        # The other session must now be revoked, but the caller stays logged in.
        self.assertEqual(other.get("/api/config").status_code, 401)
        self.assertEqual(self.client.get("/api/config").status_code, 200)

    def test_config_credential_change_invalidates_other_sessions(self):
        from fastapi.testclient import TestClient
        from mediaspecter import app
        other = TestClient(app)
        other.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        self.assertEqual(other.get("/api/config").status_code, 200)

        self.client.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        new_cfg = {
            "servers": [], "rules": {}, "safety": {},
            "security": {"enabled": True, "username": "testuser", "password": "rotated-pw"},
        }
        resp = self.client.post("/api/config", json={"config": new_cfg})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(other.get("/api/config").status_code, 401)
        self.assertEqual(self.client.get("/api/config").status_code, 200)

    def test_login_cookie_secure_follows_https_only(self):
        # Default (https_only unset) → cookie is not marked Secure.
        resp = self.client.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        self.assertNotIn("secure", resp.headers.get("set-cookie", "").lower())

        # https_only: true → Secure flag is set.
        self.specter.config["security"]["https_only"] = True
        resp = self.client.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        self.assertIn("secure", resp.headers.get("set-cookie", "").lower())

    def test_update_config_preserves_security(self):
        self.client.post("/api/login", json={"username": "testuser", "password": "testpassword"})
        # The settings form posts a config without a security block — auth must survive.
        resp = self.client.post("/api/config", json={"config": {"servers": [], "rules": {}, "safety": {}}})
        self.assertEqual(resp.status_code, 200)
        import mediaspecter
        self.assertIn("security", mediaspecter.GLOBAL_SPECTER.config)
        self.assertTrue(mediaspecter.GLOBAL_SPECTER.config["security"].get("enabled"))

    def test_logout(self):
        # Login first
        resp = self.client.post("/api/login", json={
            "username": "testuser",
            "password": "testpassword"
        })
        self.assertEqual(resp.status_code, 200)
        
        # Verify access
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        
        # Logout
        resp = self.client.post("/api/logout")
        self.assertEqual(resp.status_code, 200)
        
        # Verify access is now denied
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 401)


class TestPropagation(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()

        self.config_data = {
            "servers": [
                {"type": "plex", "enabled": True, "url": "http://mock-plex", "token": "t", "libraries": ["Movies"]},
                {"type": "jellyfin", "enabled": True, "url": "http://mock-jf", "username": "u", "password": "p", "libraries": ["Movies"]},
            ],
            "rules": {"min_age_days": 0, "exclude_labels": [], "exclude_genres": [], "dummy_threshold_mb": 0},
            "safety": {"dry_run": False, "backup_original_media": False, "allow_automated_archival": True},
            "integrations": {},
        }

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        if os.path.exists(self.temp_config.name):
            os.unlink(self.temp_config.name)

    def _make_specter(self):
        from mediaspecter import MediaSpecter
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config_data, f)
        specter = MediaSpecter(self.temp_config.name)
        specter.db = Database(self.temp_db.name)
        return specter

    def _make_specter_with_mock_servers(self, *servers):
        """Create spktor and replace servers with mock list."""
        specter = self._make_specter()
        specter.servers = list(servers)
        return specter

    @patch("mediaspecter.PlexServer")
    @patch("mediaspecter.JellyfinConnector.authenticate")
    def test_find_item_path_then_id(self, mock_jf_auth, mock_plex_server_cls):
        # Build Plex connector with mocked section/search
        mock_plex = MagicMock()
        mock_plex_server_cls.return_value = mock_plex

        import mediaspecter
        plex_cfg = {"type": "plex", "enabled": True, "url": "http://mock-plex", "token": "t", "libraries": ["Movies"]}
        plex = mediaspecter.PlexConnector(plex_cfg)

        match_item = MagicMock()
        match_item.ratingKey = 777
        match_item.media = [MagicMock()]
        match_item.media[0].parts = [MagicMock()]
        match_item.media[0].parts[0].file = "/data/Movie.mkv"
        guid_obj = MagicMock()
        guid_obj.id = "tmdb://12345"
        match_item.guids = [guid_obj]

        no_match_item = MagicMock()
        no_match_item.ratingKey = 888
        no_match_item.media = [MagicMock()]
        no_match_item.media[0].parts = [MagicMock()]
        no_match_item.media[0].parts[0].file = "/data/Other.mkv"
        no_match_item.guids = []

        mock_section = MagicMock()
        mock_section.type = "movie"
        mock_section.search.return_value = [no_match_item, match_item]
        mock_plex.library.section.return_value = mock_section

        plex._server = mock_plex
        plex.get_item_metadata = MagicMock()
        plex.get_item_metadata.return_value = {
            "id": 777, "title": "Match", "type": "movie", "file_path": "/data/Movie.mkv",
            "original_size": 100, "last_watched": None, "genres": [], "labels": [],
            "external_ids": {"tmdb": "12345", "imdb": None, "tvdb": None},
        }

        # Path match
        result = plex.find_item("/data/Movie.mkv", {}, "movie")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 777)

        # TMDB ID fallback
        result2 = plex.find_item("/data/nonexistent.mkv", {"tmdb": "12345", "imdb": None, "tvdb": None}, "movie")
        self.assertIsNotNone(result2)
        self.assertEqual(result2["id"], 777)

        # Episode with non-matching path returns None
        result3 = plex.find_item("/data/nonexistent.mkv", {}, "episode")
        self.assertIsNone(result3)

    @patch("mediaspecter.JellyfinConnector.authenticate")
    @patch("mediaspecter.PlexServer")
    def test_archive_item_propagates_to_all_servers(self, mock_plex_cls, mock_jf_auth):
        plex_item = {
            "id": "1", "title": "Movie", "type": "movie", "file_path": "/data/Movie.mp4",
            "original_size": 100_000_000, "last_watched": None, "genres": [], "labels": [],
            "external_ids": {"tmdb": "123", "imdb": "tt456", "tvdb": None},
        }
        jf_item = {
            "id": "99", "title": "Movie", "type": "movie", "file_path": "/data/Movie.mp4",
            "original_size": 100_000_000, "last_watched": None, "genres": [], "labels": [],
            "external_ids": {"tmdb": None, "imdb": "tt456", "tvdb": None},
        }

        plex = MagicMock()
        plex.server_type = "plex"
        plex.get_item_metadata.return_value = plex_item
        plex.find_item.return_value = plex_item
        plex.download_poster.return_value = True
        plex.upload_poster.return_value = True
        plex.trigger_library_scan = MagicMock()

        jf = MagicMock()
        jf.server_type = "jellyfin"
        jf.get_item_metadata.return_value = jf_item
        jf.find_item.return_value = jf_item
        jf.download_poster.return_value = True
        jf.upload_poster.return_value = True
        jf.trigger_library_scan = MagicMock()

        specter = self._make_specter_with_mock_servers(plex, jf)

        # The real filesystem swap is covered by TestSafeReplace; here we stub it
        # so propagation logic can run against fake paths.
        with patch.object(MediaSpecter, "_replace_with_dummy", return_value=None), \
             patch("mediaspecter.shutil.copy2"), \
             patch("builtins.open"):
            result = specter.archive_item("plex", "1")

        self.assertTrue(result["success"])
        jf.find_item.assert_called()
        self.assertGreaterEqual(plex.upload_poster.call_count, 1)
        self.assertGreaterEqual(jf.upload_poster.call_count, 1)
        # One physical movie archived across two servers = one counted item
        # (stats dedupe by original_path), though two DB rows exist.
        self.assertEqual(specter.db.get_stats()["total_items"], 1)
        self.assertEqual(len(specter.db.get_items_by_path("/data/Movie.mp4", status="archived")), 2)

    @patch("mediaspecter.JellyfinConnector.authenticate")
    @patch("mediaspecter.PlexServer")
    def test_archive_item_skips_unmatched_server(self, mock_plex_cls, mock_jf_auth):
        plex_item = {
            "id": "1", "title": "Movie", "type": "movie", "file_path": "/data/Movie.mp4",
            "original_size": 100_000_000, "last_watched": None, "genres": [], "labels": [],
            "external_ids": {"tmdb": "123", "imdb": None, "tvdb": None},
        }

        plex = MagicMock()
        plex.server_type = "plex"
        plex.get_item_metadata.return_value = plex_item
        plex.find_item.return_value = plex_item
        plex.download_poster.return_value = True
        plex.upload_poster.return_value = True
        plex.trigger_library_scan = MagicMock()

        jf = MagicMock()
        jf.server_type = "jellyfin"
        jf.find_item.return_value = None
        jf.trigger_library_scan = MagicMock()

        specter = self._make_specter_with_mock_servers(plex, jf)

        # The real filesystem swap is covered by TestSafeReplace; here we stub it
        # so propagation logic can run against fake paths.
        with patch.object(MediaSpecter, "_replace_with_dummy", return_value=None), \
             patch("mediaspecter.shutil.copy2"), \
             patch("builtins.open"):
            result = specter.archive_item("plex", "1")

        self.assertTrue(result["success"])
        self.assertTrue(any("jellyfin" in str(w).lower() for w in result.get("warnings", [])))
        self.assertEqual(specter.db.get_stats()["total_items"], 1)
        jf.upload_poster.assert_not_called()

    @patch("mediaspecter.JellyfinConnector.authenticate")
    @patch("mediaspecter.PlexServer")
    def test_archive_item_dry_run_does_not_touch_files(self, mock_plex_cls, mock_jf_auth):
        """Dry-Run must block the single-item (UI) archive — no file swap, no DB row."""
        self.config_data["safety"]["dry_run"] = True
        plex_item = {
            "id": "1", "title": "Movie", "type": "movie", "file_path": "/data/Movie.mp4",
            "original_size": 100_000_000, "last_watched": None, "genres": [], "labels": [],
            "external_ids": {"tmdb": "123", "imdb": "tt456", "tvdb": None},
        }
        plex = MagicMock()
        plex.server_type = "plex"
        plex.get_item_metadata.return_value = plex_item

        specter = self._make_specter_with_mock_servers(plex)

        with patch.object(MediaSpecter, "_replace_with_dummy") as mock_swap:
            result = specter.archive_item("plex", "1")

        self.assertTrue(result["success"])
        self.assertTrue(result.get("dry_run"))
        mock_swap.assert_not_called()
        plex.upload_poster.assert_not_called()
        self.assertEqual(specter.db.get_stats()["total_items"], 0)

    def test_restore_fans_out(self):
        specter = self._make_specter()

        # Seed two sibling rows
        specter.db.insert(
            server_type="plex", server_item_id="1", title="Movie", media_type="movie",
            original_path="/data/Movie.mp4", original_size_bytes=100, dummy_size_bytes=10,
            backup_poster_path="/backups/plex_1_poster_original.jpg", status="archived",
        )
        specter.db.insert(
            server_type="jellyfin", server_item_id="99", title="Movie", media_type="movie",
            original_path="/data/Movie.mp4", original_size_bytes=100, dummy_size_bytes=10,
            backup_poster_path="/backups/jellyfin_99_poster_original.jpg", status="archived",
        )

        plex = MagicMock()
        plex.server_type = "plex"
        plex.upload_poster.return_value = True
        plex.trigger_library_scan = MagicMock()

        jf = MagicMock()
        jf.server_type = "jellyfin"
        jf.upload_poster.return_value = True
        jf.trigger_library_scan = MagicMock()

        specter.servers = [plex, jf]

        with patch("os.path.exists", return_value=True), patch("shutil.move"):
            self.assertTrue(specter.restore("plex", "1"))

        # Both rows restored
        self.assertEqual(specter.db.get_item("plex", "1")["status"], "restored")
        self.assertEqual(specter.db.get_item("jellyfin", "99")["status"], "restored")
        # Upload called for both
        plex.upload_poster.assert_called()
        jf.upload_poster.assert_called()

    def test_expand_external_ids_bridges(self):
        from mediaspecter import TmdbClient

        tmdb = TmdbClient("fake-key-not-jwt")

        def fake_get(path, **params):
            if "/find/" in path:
                return {"movie_results": [{"id": 99}]}
            if "/external_ids" in path:
                return {"imdb_id": "tt789", "tvdb_id": 2020}
            return {}

        tmdb._get = fake_get

        specter = self._make_specter()
        specter.tmdb = tmdb

        # Expand from imdb only
        result = specter._expand_external_ids("movie", {"tmdb": None, "imdb": "tt123", "tvdb": None})
        self.assertEqual(result["tmdb"], "99")
        self.assertEqual(result["imdb"], "tt123")
        self.assertEqual(result["tvdb"], "2020")

        # Episodes return input unchanged
        ep_result = specter._expand_external_ids("episode", {"tmdb": None, "imdb": "tt123"})
        self.assertEqual(ep_result["tmdb"], None)

        # When disabled, return input unchanged
        tmdb.api_key = ""
        result3 = specter._expand_external_ids("movie", {"tmdb": None, "imdb": "tt123", "tvdb": None})
        self.assertEqual(result3["tmdb"], None)


class TestSafeReplace(unittest.TestCase):
    """Guards the data-safety contract of _replace_with_dummy."""

    def _specter(self):
        # Bare instance — _replace_with_dummy needs no config/connectors.
        return MediaSpecter.__new__(MediaSpecter)

    def test_atomic_replace(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "movie.mkv")
            with open(f, "wb") as fh:
                fh.write(b"X" * 1000)
            out = self._specter()._replace_with_dummy(f, b"DUMMY")
            self.assertIsNone(out)
            with open(f, "rb") as fh:
                self.assertEqual(fh.read(), b"DUMMY")

    def test_replace_with_backup_preserves_original(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "movie.mkv")
            backup = os.path.join(d, "plex_1.mkv")
            with open(f, "wb") as fh:
                fh.write(b"ORIGINAL")
            out = self._specter()._replace_with_dummy(f, b"DUMMY", backup)
            self.assertEqual(out, backup)
            with open(f, "rb") as fh:
                self.assertEqual(fh.read(), b"DUMMY")
            with open(backup, "rb") as fh:
                self.assertEqual(fh.read(), b"ORIGINAL")

    def test_missing_file_raises_without_side_effects(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "does-not-exist.mkv")
            with self.assertRaises(FileNotFoundError):
                self._specter()._replace_with_dummy(f, b"DUMMY")
            self.assertFalse(os.path.exists(f))
            # No stray temp files left behind in the directory
            self.assertEqual(os.listdir(d), [])

    def test_clones_original_permissions(self):
        # mkstemp makes 0600; the dummy must inherit the original's mode so a
        # media server running as another user can still read it.
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "movie.mkv")
            with open(f, "wb") as fh:
                fh.write(b"X" * 100)
            os.chmod(f, 0o644)
            self._specter()._replace_with_dummy(f, b"DUMMY")
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o644)


class TestBackupDirFallback(unittest.TestCase):
    def test_placeholder_backup_dir_falls_back_to_config_dir(self):
        # The seeded example placeholder must not crash startup (it isn't writable);
        # it should fall back to <config dir>/backups.
        import yaml
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                yaml.safe_dump(
                    {"servers": [], "safety": {"backup_directory": "/path/to/cold/storage/backup"}}, f
                )
            specter = MediaSpecter(cfg)
            self.assertEqual(str(specter.backup_dir), os.path.join(d, "backups"))
            self.assertTrue(os.path.isdir(specter.backup_dir))


class TestRegenerate(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        
        self.config_data = {
            "servers": [
                {
                    "type": "plex",
                    "enabled": True,
                    "url": "http://mock-plex",
                    "token": "mock-token",
                    "libraries": ["Movies"]
                }
            ],
            "rules": {
                "min_age_days": 7,
                "exclude_labels": [],
                "exclude_genres": [],
                "dummy_threshold_mb": 15
            },
            "safety": {
                "dry_run": True,
                "backup_original_media": False,
                "backup_directory": "./backups"
            }
        }
        
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config_data, f)
            
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)
        
        self.mock_connector = MagicMock()
        self.mock_connector.server_type = "plex"
        self.specter.servers = [self.mock_connector]

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        if os.path.exists(self.temp_config.name):
            os.unlink(self.temp_config.name)

    def test_regenerate_item_not_found(self):
        res = self.specter.regenerate_item("plex", "99999", "poster")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "Item not found in database.")

    def test_regenerate_poster_backup_missing(self):
        self.specter.db.insert(
            server_type="plex",
            server_item_id="12345",
            title="Test Movie",
            media_type="movie",
            original_path="/path/to/movie.mkv",
            original_size_bytes=10 * 1024 * 1024 * 1024,
            dummy_size_bytes=2048,
            backup_poster_path="/nonexistent/poster.jpg",
            backup_media_path=None,
            status="archived"
        )
        res = self.specter.regenerate_item("plex", "12345", "poster")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "Original poster backup not found.")

    def test_regenerate_poster_success(self):
        with tempfile.TemporaryDirectory() as d:
            poster_backup = os.path.join(d, "plex_12345_poster_original.jpg")
            with open(poster_backup, "wb") as f:
                f.write(b"fake image data")
            
            self.specter.db.insert(
                server_type="plex",
                server_item_id="12345",
                title="Test Movie",
                media_type="movie",
                original_path="/path/to/movie.mkv",
                original_size_bytes=10 * 1024 * 1024 * 1024,
                dummy_size_bytes=2048,
                backup_poster_path=poster_backup,
                backup_media_path=None,
                status="archived"
            )
            
            self.specter.overlay.apply_overlay = MagicMock(return_value=True)
            self.mock_connector.upload_poster.return_value = True
            
            res = self.specter.regenerate_item("plex", "12345", "poster")
            self.assertTrue(res["success"])
            self.specter.overlay.apply_overlay.assert_called_once()
            self.mock_connector.upload_poster.assert_called_once()
            self.assertIn("Regenerated poster for plex", res["messages"])

    def test_regenerate_video_success(self):
        with tempfile.TemporaryDirectory() as d:
            movie_path = os.path.join(d, "movie.mkv")
            with open(movie_path, "wb") as f:
                f.write(b"original large movie data")
            
            self.specter.db.insert(
                server_type="plex",
                server_item_id="12345",
                title="Test Movie",
                media_type="movie",
                original_path=movie_path,
                original_size_bytes=10 * 1024 * 1024 * 1024,
                dummy_size_bytes=2048,
                backup_poster_path=None,
                backup_media_path=None,
                status="archived"
            )
            
            res = self.specter.regenerate_item("plex", "12345", "video")
            self.assertTrue(res["success"])
            self.assertIn("Dummy video regenerated and permissions applied.", res["messages"])
            with open(movie_path, "rb") as f:
                content = f.read()
            self.assertNotEqual(content, b"original large movie data")

    def test_regenerate_video_refuses_non_archived(self):
        with tempfile.TemporaryDirectory() as d:
            movie_path = os.path.join(d, "movie.mkv")
            with open(movie_path, "wb") as f:
                f.write(b"restored real media")

            self.specter.db.insert(
                server_type="plex",
                server_item_id="12345",
                title="Test Movie",
                media_type="movie",
                original_path=movie_path,
                original_size_bytes=10 * 1024 * 1024 * 1024,
                dummy_size_bytes=2048,
                backup_poster_path=None,
                backup_media_path=None,
                status="restored",
            )

            res = self.specter.regenerate_item("plex", "12345", "video")
            self.assertFalse(res["success"])
            self.assertIn("not 'archived'", res["error"])
            # File must be untouched.
            with open(movie_path, "rb") as f:
                self.assertEqual(f.read(), b"restored real media")

    def test_regenerate_video_refuses_large_file(self):
        with tempfile.TemporaryDirectory() as d:
            movie_path = os.path.join(d, "movie.mkv")
            with open(movie_path, "wb") as f:
                f.seek(60 * 1024 * 1024)  # 60 MB sparse file > safety ceiling
                f.write(b"\0")

            self.specter.db.insert(
                server_type="plex",
                server_item_id="12345",
                title="Test Movie",
                media_type="movie",
                original_path=movie_path,
                original_size_bytes=10 * 1024 * 1024 * 1024,
                dummy_size_bytes=2048,
                backup_poster_path=None,
                backup_media_path=None,
                status="archived",
            )

            res = self.specter.regenerate_item("plex", "12345", "video")
            self.assertFalse(res["success"])
            self.assertIn("real media", res["error"])
            self.assertTrue(os.path.getsize(movie_path) > 50 * 1024 * 1024)


class TestPosterUploadEncoding(unittest.TestCase):
    """Jellyfin/Emby expect the image body Base64-encoded; raw bytes -> HTTP 500."""

    def _write_poster(self, d):
        p = os.path.join(d, "overlay.jpg")
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0 fake jpeg bytes \x00\x01\x02")
        return p

    def test_jellyfin_uploads_base64_body(self):
        import mediaspecter
        with patch.object(mediaspecter.JellyfinConnector, "authenticate"):
            jf = mediaspecter.JellyfinConnector(
                {"type": "jellyfin", "url": "http://jf", "username": "u", "password": "p"}
            )
        jf.api_key = "key"
        with tempfile.TemporaryDirectory() as d:
            poster = self._write_poster(d)
            raw = open(poster, "rb").read()
            with patch.object(jf, "_request") as mock_req:
                self.assertTrue(jf.upload_poster("itm", poster))
            sent = mock_req.call_args.kwargs["data"]
            self.assertEqual(sent, base64.b64encode(raw))
            self.assertEqual(mock_req.call_args.kwargs["req_headers"]["Content-Type"], "image/jpeg")

    def test_emby_uploads_base64_body(self):
        import mediaspecter
        with patch.object(mediaspecter.EmbyConnector, "_resolve_user_id"):
            emby = mediaspecter.EmbyConnector(
                {"type": "emby", "url": "http://emby", "api_key": "k", "user_id": "uid"}
            )
        with tempfile.TemporaryDirectory() as d:
            poster = self._write_poster(d)
            raw = open(poster, "rb").read()
            with patch("mediaspecter.requests.post") as mock_post:
                mock_post.return_value = MagicMock(raise_for_status=lambda: None)
                self.assertTrue(emby.upload_poster("itm", poster))
            self.assertEqual(mock_post.call_args.kwargs["data"], base64.b64encode(raw))


class TestRadarrMatching(unittest.TestCase):
    """Radarr match must survive Radarr and the media server mounting different roots."""

    MOVIES = [
        {"id": 42, "title": "The Calendar Killer", "tmdbId": 1234, "imdbId": "tt9999",
         "path": "/movies/The Calendar Killer (2024)", "monitored": True},
        {"id": 99, "title": "Other", "tmdbId": 5678, "imdbId": "tt1111",
         "path": "/movies/Other (2023)", "monitored": True},
    ]

    def test_match_by_tmdb_when_paths_differ(self):
        # Media server reports /data/..., Radarr reports /movies/... — only IDs align.
        m = RadarrClient._match_movie(
            self.MOVIES, "/data/media/The Calendar Killer (2024)/film.mkv",
            {"tmdb": "1234"},
        )
        self.assertIsNotNone(m)
        self.assertEqual(m["id"], 42)

    def test_match_by_imdb(self):
        m = RadarrClient._match_movie(self.MOVIES, "/x/y.mkv", {"imdb": "tt9999"})
        self.assertEqual(m["id"], 42)

    def test_match_by_folder_leaf_when_root_differs(self):
        # No IDs, but the movie folder name matches despite a different root.
        m = RadarrClient._match_movie(
            self.MOVIES, "/data/The Calendar Killer (2024)/film.mkv", {},
        )
        self.assertEqual(m["id"], 42)

    def test_no_match_returns_none(self):
        self.assertIsNone(
            RadarrClient._match_movie(self.MOVIES, "/data/Nope (2020)/f.mkv", {"tmdb": "0"})
        )


class TestMonitorToggle(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)
        self.specter.db.insert(
            server_type="plex", server_item_id="1", title="Movie", media_type="movie",
            original_path="/data/Movie.mkv", original_size_bytes=10**10, dummy_size_bytes=2048,
            backup_poster_path=None, backup_media_path=None, status="archived",
        )

    def tearDown(self):
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_set_monitor_requires_radarr(self):
        self.specter.radarr = None
        res = self.specter.set_item_monitor("plex", "1", True)
        self.assertFalse(res["success"])
        self.assertIn("Radarr", res["error"])

    def test_set_monitor_calls_radarr(self):
        self.specter.radarr = MagicMock()
        self.specter.radarr.set_movie_monitored.return_value = True
        res = self.specter.set_item_monitor("plex", "1", True)
        self.assertTrue(res["success"])
        self.specter.radarr.set_movie_monitored.assert_called_once_with("/data/Movie.mkv", True)

    def test_get_monitor_state(self):
        self.specter.radarr = MagicMock()
        self.specter.radarr.get_movie_monitored.return_value = False
        res = self.specter.get_item_monitor("plex", "1")
        self.assertTrue(res["available"])
        self.assertFalse(res["monitored"])


class TestLibraryScanScoping(unittest.TestCase):
    """Scans should target the changed item's library/type, not the whole server."""

    def test_plex_scan_scopes_to_media_type(self):
        import mediaspecter
        with patch("mediaspecter.PlexServer"):
            plex = mediaspecter.PlexConnector(
                {"type": "plex", "url": "http://p", "token": "t", "libraries": ["Movies", "TV"]}
            )
        movie_sec, tv_sec = MagicMock(), MagicMock()
        movie_sec.type, tv_sec.type = "movie", "show"
        sections = {"Movies": movie_sec, "TV": tv_sec}
        plex._server = MagicMock()
        plex._server.library.section.side_effect = lambda n: sections[n]

        plex.trigger_library_scan(media_type="movie")
        movie_sec.update.assert_called_once()
        tv_sec.update.assert_not_called()

    def test_jellyfin_scan_refreshes_single_item(self):
        import mediaspecter
        with patch.object(mediaspecter.JellyfinConnector, "authenticate"):
            jf = mediaspecter.JellyfinConnector(
                {"type": "jellyfin", "url": "http://jf", "username": "u", "password": "p"}
            )
        jf.api_key = "key"
        with patch.object(jf, "_request") as mock_req:
            jf.trigger_library_scan(media_type="movie", item_id="42")
        path = mock_req.call_args.args[1]
        self.assertEqual(path, "Items/42/Refresh")
        # Never touches images, so the badged poster survives.
        self.assertEqual(mock_req.call_args.kwargs["params"]["ImageRefreshMode"], "None")

    def test_jellyfin_scan_falls_back_to_full_when_no_item(self):
        import mediaspecter
        with patch.object(mediaspecter.JellyfinConnector, "authenticate"):
            jf = mediaspecter.JellyfinConnector(
                {"type": "jellyfin", "url": "http://jf", "username": "u", "password": "p"}
            )
        jf.api_key = "key"
        with patch.object(jf, "_request") as mock_req:
            jf.trigger_library_scan()
        self.assertEqual(mock_req.call_args.args[1], "Library/Refresh")


class TestBulkPlan(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)

    def tearDown(self):
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_bulk_episode_plan_counts(self):
        mock = MagicMock()
        mock.server_type = "plex"
        mock.get_seasons.return_value = [{"id": "s1"}]
        mock.get_episodes.return_value = [
            {"id": "1", "original_size": 100_000_000, "is_watched": True},
            {"id": "2", "original_size": 200_000_000, "is_watched": False},
            {"id": "3", "original_size": 0, "is_watched": True},
        ]
        self.specter.servers = [mock]
        self.specter.db.item_exists = MagicMock(return_value=False)

        plan = self.specter.bulk_episode_plan("plex", "show1")
        self.assertEqual(plan["count"], 3)
        self.assertEqual(plan["unwatched"], 1)
        self.assertEqual(plan["total_size_bytes"], 300_000_000)
        self.assertEqual(plan["already_archived"], 0)

    def test_bulk_episode_plan_excludes_archived(self):
        mock = MagicMock()
        mock.server_type = "plex"
        mock.get_seasons.return_value = [{"id": "s1"}]
        mock.get_episodes.return_value = [
            {"id": "1", "original_size": 100_000_000, "is_watched": True},
            {"id": "2", "original_size": 200_000_000, "is_watched": False},
        ]
        self.specter.servers = [mock]
        self.specter.db.item_exists = lambda st, iid: iid == "1"

        plan = self.specter.bulk_episode_plan("plex", "show1")
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["already_archived"], 1)
        self.assertEqual(plan["item_ids"], ["2"])


class TestBulkArchive(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)

    def tearDown(self):
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_bulk_archive_calls_archive_item(self):
        self.specter.archive_item = MagicMock(return_value={"success": True})
        res = self.specter.bulk_archive("plex", ["1", "2"])
        self.assertEqual(self.specter.archive_item.call_count, 2)
        self.assertEqual(len(res["archived"]), 2)
        self.assertEqual(len(res["errors"]), 0)

    def test_bulk_archive_reports_errors(self):
        def side_effect(st, iid, skip_scan=False):
            if iid == "2":
                return {"success": False, "error": "fail"}
            return {"success": True}
        self.specter.archive_item = MagicMock(side_effect=side_effect)
        res = self.specter.bulk_archive("plex", ["1", "2"])
        self.assertEqual(len(res["archived"]), 1)
        self.assertEqual(len(res["errors"]), 1)
        self.assertEqual(res["errors"][0]["item_id"], "2")


class TestShowTotalSize(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)

    def tearDown(self):
        import mediaspecter
        mediaspecter._SHOW_SIZE_CACHE.clear()
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_get_show_total_size_populates_cache(self):
        import mediaspecter
        mock = MagicMock()
        mock.server_type = "plex"
        mock.get_shows.return_value = [{"id": "s1", "title": "Show", "year": 2025}]
        mock.get_show_total_size.return_value = 500_000_000
        mock.config = {"libraries": ["TV"]}
        self.specter.servers = [mock]

        from fastapi.testclient import TestClient
        from mediaspecter import app
        old_specter = mediaspecter.GLOBAL_SPECTER
        mediaspecter.GLOBAL_SPECTER = self.specter
        try:
            client = TestClient(app)
            resp = client.get("/api/shows")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["total_size"], 500_000_000)
            self.assertIn(("plex", "s1"), mediaspecter._SHOW_SIZE_CACHE)
        finally:
            mediaspecter.GLOBAL_SPECTER = old_specter

    def test_plex_get_show_total_size(self):
        import mediaspecter
        with patch("mediaspecter.PlexServer") as mock_ps:
            mock_se = MagicMock()
            mock_show = MagicMock()
            ep1 = MagicMock()
            ep1.media = [MagicMock()]
            ep1.media[0].parts = [MagicMock()]
            ep1.media[0].parts[0].size = 100
            ep2 = MagicMock()
            ep2.media = [MagicMock()]
            ep2.media[0].parts = [MagicMock()]
            ep2.media[0].parts[0].size = 200
            mock_show.episodes.return_value = [ep1, ep2]
            mock_ps.return_value.library = MagicMock()
            mock_se.fetchItem.return_value = mock_show

            plex = mediaspecter.PlexConnector(
                {"type": "plex", "url": "http://p", "token": "t", "libraries": ["TV"]}
            )
            plex._server = mock_se
            total = plex.get_show_total_size("123")
            self.assertEqual(total, 300)


class TestShowMatching(unittest.TestCase):
    def test_normalize_show_title(self):
        from mediaspecter import _normalize_show_title
        self.assertEqual(_normalize_show_title("FROM"), "from")
        self.assertEqual(_normalize_show_title("From"), "from")
        self.assertEqual(_normalize_show_title("The Office (US)"), "the office")
        self.assertEqual(_normalize_show_title("Yellowstone (2018)"), "yellowstone")
        self.assertEqual(_normalize_show_title("Poker Face (2023)"), "poker face")

    def test_titles_match(self):
        from mediaspecter import _titles_match
        self.assertTrue(_titles_match("FROM", "From"))
        self.assertTrue(_titles_match("Yellowstone (2018)", "Yellowstone", 2018, None))
        self.assertFalse(_titles_match("The Office", "The Office", 2005, 2001))
        self.assertFalse(_titles_match("Stargirl", "DC's Stargirl"))

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)

    def tearDown(self):
        import mediaspecter
        mediaspecter._SHOW_SIZE_CACHE.clear()
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_shows_id_match_dedupes(self):
        import mediaspecter
        srv_a = MagicMock()
        srv_a.server_type = "plex"
        srv_a.config = {"libraries": ["TV"]}
        srv_a.get_shows.return_value = [{
            "id": "a1", "title": "Stargirl", "year": 2020,
            "external_ids": {"tvdb": "385376", "imdb": None, "tmdb": None}
        }]
        srv_a.get_show_total_size.return_value = 0
        srv_b = MagicMock()
        srv_b.server_type = "jellyfin"
        srv_b.config = {"libraries": ["TV"]}
        srv_b.get_shows.return_value = [{
            "id": "b1", "title": "DC's Stargirl", "year": 2020,
            "external_ids": {"tvdb": "385376", "imdb": None, "tmdb": None}
        }]
        srv_b.get_show_total_size.return_value = 0
        self.specter.servers = [srv_a, srv_b]

        from fastapi.testclient import TestClient
        from mediaspecter import app
        old = mediaspecter.GLOBAL_SPECTER
        mediaspecter.GLOBAL_SPECTER = self.specter
        try:
            client = TestClient(app)
            resp = client.get("/api/shows")
            data = resp.json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["title"], "Stargirl")
        finally:
            mediaspecter.GLOBAL_SPECTER = old

    def test_shows_title_fallback_dedupes(self):
        import mediaspecter
        srv_a = MagicMock()
        srv_a.server_type = "plex"
        srv_a.config = {"libraries": ["TV"]}
        srv_a.get_shows.return_value = [{
            "id": "a1", "title": "Yellowstone (2018)", "year": 2018,
            "external_ids": {}
        }]
        srv_a.get_show_total_size.return_value = 0
        srv_b = MagicMock()
        srv_b.server_type = "jellyfin"
        srv_b.config = {"libraries": ["TV"]}
        srv_b.get_shows.return_value = [{
            "id": "b1", "title": "Yellowstone", "year": None,
            "external_ids": {}
        }]
        srv_b.get_show_total_size.return_value = 0
        self.specter.servers = [srv_a, srv_b]

        from fastapi.testclient import TestClient
        from mediaspecter import app
        old = mediaspecter.GLOBAL_SPECTER
        mediaspecter.GLOBAL_SPECTER = self.specter
        try:
            client = TestClient(app)
            resp = client.get("/api/shows")
            data = resp.json()
            self.assertEqual(len(data), 1)
        finally:
            mediaspecter.GLOBAL_SPECTER = old

    def test_shows_distinct_remain_separate(self):
        import mediaspecter
        srv = MagicMock()
        srv.server_type = "plex"
        srv.config = {"libraries": ["TV"]}
        srv.get_shows.return_value = [
            {"id": "a1", "title": "Breaking Bad", "year": 2008, "external_ids": {"tvdb": "81189"}},
            {"id": "a2", "title": "Better Call Saul", "year": 2015, "external_ids": {"tvdb": "273181"}},
        ]
        srv.get_show_total_size.return_value = 0
        self.specter.servers = [srv]

        from fastapi.testclient import TestClient
        from mediaspecter import app
        old = mediaspecter.GLOBAL_SPECTER
        mediaspecter.GLOBAL_SPECTER = self.specter
        try:
            client = TestClient(app)
            resp = client.get("/api/shows")
            data = resp.json()
            self.assertEqual(len(data), 2)
        finally:
            mediaspecter.GLOBAL_SPECTER = old


class TestRollupBadges(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)

    def tearDown(self):
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_rollup_db_methods(self):
        self.specter.db.add_rollup_badge("plex", "123", "season", "/backups/plex_123_season_original.jpg")
        row = self.specter.db.get_rollup_badge("plex", "123")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "season")
        self.assertEqual(row["backup_poster_path"], "/backups/plex_123_season_original.jpg")
        self.specter.db.remove_rollup_badge("plex", "123")
        self.assertIsNone(self.specter.db.get_rollup_badge("plex", "123"))

    def test_sonarr_season_ended(self):
        past = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # all past -> True
        self.assertTrue(MediaSpecter._sonarr_season_ended(
            [{"seasonNumber": 1, "airDateUtc": past}], 1))
        # future -> False
        self.assertFalse(MediaSpecter._sonarr_season_ended(
            [{"seasonNumber": 1, "airDateUtc": past}, {"seasonNumber": 1, "airDateUtc": future}], 1))
        # null airDateUtc -> False
        self.assertFalse(MediaSpecter._sonarr_season_ended(
            [{"seasonNumber": 1, "airDateUtc": None}], 1))
        # empty -> False
        self.assertFalse(MediaSpecter._sonarr_season_ended([], 1))

    def test_reconcile_badges_season_and_series(self):
        show = {"id": "s1", "title": "Test Show", "year": 2020, "external_ids": {"tvdb": "123"}}
        season = {"id": "sea1", "season_number": 1}
        ep1 = {"id": "e1", "original_size": 100_000_000}
        ep2 = {"id": "e2", "original_size": 200_000_000}
        self.specter.db.insert(
            server_type="plex", server_item_id="e1", title="E1", media_type="episode",
            original_path="/tv/e1.mkv", original_size_bytes=100_000_000, dummy_size_bytes=1024,
            status="archived"
        )
        self.specter.db.insert(
            server_type="plex", server_item_id="e2", title="E2", media_type="episode",
            original_path="/tv/e2.mkv", original_size_bytes=200_000_000, dummy_size_bytes=1024,
            status="archived"
        )

        mock = MagicMock()
        mock.server_type = "plex"
        mock.config = {"libraries": ["TV"]}
        mock.get_shows.return_value = [show]
        mock.get_seasons.return_value = [season]
        mock.get_episodes.return_value = [ep1, ep2]
        mock.download_poster.return_value = True
        mock.upload_poster.return_value = True
        self.specter.servers = [mock]

        self.specter.overlay.apply_overlay = MagicMock(return_value=True)
        self.specter.sonarr = MagicMock()
        self.specter.sonarr.find_series.return_value = {"id": 10, "status": "ended"}
        past = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.specter.sonarr.get_series_episodes.return_value = [
            {"seasonNumber": 1, "airDateUtc": past},
        ]

        with patch("mediaspecter.shutil.copy2"), patch("os.path.exists", return_value=False):
            res = self.specter.reconcile_rollup_badges()
        self.assertIn("plex:season:sea1", res["badged"])
        self.assertIn("plex:series:s1", res["badged"])
        self.assertIsNotNone(self.specter.db.get_rollup_badge("plex", "sea1"))
        self.assertIsNotNone(self.specter.db.get_rollup_badge("plex", "s1"))

    def test_reconcile_unbadges_when_not_fully_archived(self):
        show = {"id": "s1", "title": "Show", "year": 2020, "external_ids": {"tvdb": "123"}}
        season = {"id": "sea1", "season_number": 1}
        self.specter.db.insert(
            server_type="plex", server_item_id="e1", title="E1", media_type="episode",
            original_path="/tv/e1.mkv", original_size_bytes=100_000_000, dummy_size_bytes=1024,
            status="archived"
        )
        # e2 NOT archived -> season not fully archived
        ep1 = {"id": "e1", "original_size": 100_000_000}
        ep2 = {"id": "e2", "original_size": 200_000_000}
        backup_path = str(self.specter.backup_dir / "plex_sea1_season_original.jpg")
        with open(backup_path, "w") as f:
            f.write("fake")
        self.specter.db.add_rollup_badge("plex", "sea1", "season", backup_path)

        mock = MagicMock()
        mock.server_type = "plex"
        mock.config = {"libraries": ["TV"]}
        mock.get_shows.return_value = [show]
        mock.get_seasons.return_value = [season]
        mock.get_episodes.return_value = [ep1, ep2]
        mock.download_poster.return_value = True
        mock.upload_poster.return_value = True
        self.specter.servers = [mock]

        self.specter.sonarr = MagicMock()
        self.specter.sonarr.find_series.return_value = {"id": 10, "status": "ended"}
        past = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.specter.sonarr.get_series_episodes.return_value = [
            {"seasonNumber": 1, "airDateUtc": past},
        ]

        res = self.specter.reconcile_rollup_badges()
        self.assertIn("plex:season:sea1", res["unbadged"])
        self.assertIsNone(self.specter.db.get_rollup_badge("plex", "sea1"))
        mock.upload_poster.assert_called_with("sea1", backup_path)

    def test_reconcile_skips_without_sonarr(self):
        self.specter.sonarr = None
        res = self.specter.reconcile_rollup_badges()
        self.assertIn("skipped", res)
        self.assertEqual(res["skipped"], "Sonarr not configured")


class TestPhase2(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)

    def tearDown(self):
        import mediaspecter
        mediaspecter._SHOW_SIZE_CACHE.clear()
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_new_poster_tmp_inside_backup_dir(self):
        p1 = self.specter._new_poster_tmp()
        p2 = self.specter._new_poster_tmp()
        backup_str = str(self.specter.backup_dir)
        self.assertTrue(p1.startswith(backup_str))
        self.assertTrue(p2.startswith(backup_str))
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.endswith(".jpg"))
        self.assertTrue(p2.endswith(".jpg"))
        for p in (p1, p2):
            if os.path.exists(p):
                os.unlink(p)

    def test_safe_component_blocks_path_traversal(self):
        from mediaspecter import _safe_component
        result = _safe_component("../../etc/passwd")
        self.assertNotIn("/", result)
        self.assertTrue(len(result) > 0)

    def test_bulk_archive_skip_scan(self):
        mock = MagicMock()
        mock.server_type = "plex"
        self.specter.servers = [mock]
        calls = []
        def record(server_type, item_id, skip_scan=False):
            calls.append((server_type, item_id, skip_scan))
            return {"success": True}
        self.specter.archive_item = MagicMock(side_effect=record)
        self.specter.bulk_archive("plex", ["1", "2", "3"])
        self.assertEqual(len(calls), 3)
        for _, _, skip_scan in calls:
            self.assertTrue(skip_scan)
        mock.trigger_library_scan.assert_called_once_with(media_type="episode")

    def test_get_show_returns_external_ids(self):
        from mediaspecter import PlexConnector
        mock_show = MagicMock()
        mock_show.ratingKey = "99"
        mock_show.title = "Test"
        mock_show.year = 2020
        mock_guid = MagicMock()
        mock_guid.id = "tvdb://12345"
        mock_show.guids = [mock_guid]
        connector = object.__new__(PlexConnector)
        connector._server = MagicMock()
        connector._server.fetchItem.return_value = mock_show
        connector._resolve_id = lambda x: x
        result = connector.get_show("99")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "99")
        self.assertEqual(result["title"], "Test")
        self.assertEqual(result["year"], 2020)
        self.assertEqual(result["external_ids"]["tvdb"], "12345")

    def test_fix_rollup_no_sonarr(self):
        self.specter.sonarr = None
        mock_srv = MagicMock()
        mock_srv.server_type = "plex"
        mock_srv.get_show.return_value = {"id": "s1", "title": "T", "year": 2020, "external_ids": {"tvdb": "1"}}
        self.specter.servers = [mock_srv]
        res = self.specter.fix_rollup("plex", "99")
        self.assertIn("error", res)
        self.assertEqual(res["error"], "Sonarr not configured.")

    def test_fix_rollup_force_reupload(self):
        mock = MagicMock()
        mock.server_type = "plex"
        mock.config = {"libraries": ["TV"]}
        show = {"id": "s1", "title": "Test Show", "year": 2020, "external_ids": {"tvdb": "123"}}
        mock.get_show.return_value = show
        mock.get_seasons.return_value = [{"id": "sea1", "season_number": 1}]
        mock.get_episodes.return_value = [{"id": "e1", "original_size": 100_000_000}]
        mock.download_poster.return_value = True
        mock.upload_poster.return_value = True
        self.specter.servers = [mock]

        self.specter.db.insert(
            server_type="plex", server_item_id="e1", title="E1", media_type="episode",
            original_path="/tv/e1.mkv", original_size_bytes=100_000_000, dummy_size_bytes=1024,
            status="archived"
        )
        self.specter.db.add_rollup_badge("plex", "sea1", "season", "/backups/old.jpg")

        # The force path re-uploads the EXISTING overlay, which must be on disk at
        # the path the code derives (sanitized server_type + item_id). Create it so
        # the test is deterministic regardless of run order / shared backup dir.
        overlay_path = self.specter.backup_dir / "plex_sea1_season_overlay.jpg"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text("fake-overlay")

        self.specter.overlay.apply_overlay = MagicMock(return_value=True)
        self.specter.sonarr = MagicMock()
        self.specter.sonarr.find_series.return_value = {"id": 10, "status": "ended"}
        past = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.specter.sonarr.get_series_episodes.return_value = [
            {"seasonNumber": 1, "airDateUtc": past},
        ]

        res = self.specter.fix_rollup("plex", "s1", only_season=1)
        self.assertIn("plex:season:sea1", res["badged"])
        mock.upload_poster.assert_called_with("sea1", str(overlay_path))


class TestManualMatches(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.temp_config.close()
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump({"servers": [], "rules": {}, "safety": {"dry_run": True}}, f)
        self.specter = MediaSpecter(self.temp_config.name)
        self.specter.db = Database(self.temp_db.name)

    def tearDown(self):
        for p in (self.temp_db.name, self.temp_config.name):
            if os.path.exists(p):
                os.unlink(p)

    def test_link_manual_match_creates_group(self):
        db = self.specter.db
        gid = db.link_manual_match("movie", [("plex", "1", "A"), ("jellyfin", "2", "B")])
        self.assertIsNotNone(gid)
        members = db.get_manual_group_members("plex", "1")
        self.assertEqual(len(members), 2)
        self.assertEqual(db.get_manual_match_target("plex", "1", "jellyfin"), "2")
        self.assertEqual(db.get_manual_match_target("jellyfin", "2", "plex"), "1")

    def test_link_manual_match_merge_groups(self):
        db = self.specter.db
        gid1 = db.link_manual_match("movie", [("plex", "1", "A"), ("jellyfin", "2", "B")])
        gid2 = db.link_manual_match("movie", [("jellyfin", "2", "B"), ("emby", "3", "C")])
        self.assertEqual(gid1, gid2)
        members = db.get_manual_group_members("plex", "1")
        self.assertEqual(len(members), 3)

    def test_remove_manual_group(self):
        db = self.specter.db
        gid = db.link_manual_match("movie", [("plex", "1", "A"), ("jellyfin", "2", "B")])
        db.remove_manual_group(gid)
        self.assertEqual(db.get_manual_match_target("plex", "1", "jellyfin"), None)

    def test_attach_presence_single_server_no_missing(self):
        from mediaspecter import _attach_presence
        items = [{"server_type": "plex", "id": "1", "server_items": {"plex": "1"}}]
        _attach_presence(self.specter, items, ["plex"], "movie")
        self.assertEqual(items[0]["present_servers"], ["plex"])
        self.assertEqual(items[0]["missing_servers"], [])

    def test_movies_endpoint_missing_servers(self):
        self.config_data = {
            "servers": [
                {"type": "plex", "enabled": True, "url": "http://p", "token": "t", "libraries": ["Movies"]},
                {"type": "jellyfin", "enabled": True, "url": "http://j", "username": "u", "password": "p", "libraries": ["Movies"]},
            ],
            "rules": {}, "safety": {"dry_run": True}, "integrations": {},
        }
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config_data, f)
        specter = MediaSpecter(self.temp_config.name)
        specter.db = self.specter.db

        mock_plex = MagicMock()
        mock_plex.server_type = "plex"
        mock_plex.config = {"libraries": ["Movies"]}
        mock_plex.get_movies.return_value = [
            {"id": "m1", "title": "OnlyOnPlex", "year": 2020, "file_path": "/m/m1.mkv", "original_size": 1000}
        ]
        mock_jf = MagicMock()
        mock_jf.server_type = "jellyfin"
        mock_jf.config = {"libraries": ["Movies"]}
        mock_jf.get_movies.return_value = []
        specter.servers = [mock_plex, mock_jf]

        import mediaspecter
        old = mediaspecter.GLOBAL_SPECTER
        mediaspecter.GLOBAL_SPECTER = specter
        try:
            from fastapi.testclient import TestClient
            from mediaspecter import app
            client = TestClient(app)
            resp = client.get("/api/movies")
            movies = resp.json()
            if movies:
                self.assertIn("server_items", movies[0])
                self.assertIn("missing_servers", movies[0])
                self.assertIn("jellyfin", movies[0]["missing_servers"])
        finally:
            mediaspecter.GLOBAL_SPECTER = old

    def test_manual_match_folds_presence(self):
        db = self.specter.db
        db.link_manual_match("movie", [("plex", "m1", "A"), ("jellyfin", "m99", "B")])

        self.config_data = {
            "servers": [
                {"type": "plex", "enabled": True, "url": "http://p", "token": "t", "libraries": ["Movies"]},
                {"type": "jellyfin", "enabled": True, "url": "http://j", "username": "u", "password": "p", "libraries": ["Movies"]},
            ],
            "rules": {}, "safety": {"dry_run": True}, "integrations": {},
        }
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config_data, f)
        specter = MediaSpecter(self.temp_config.name)
        specter.db = db

        mock_plex = MagicMock()
        mock_plex.server_type = "plex"
        mock_plex.config = {"libraries": ["Movies"]}
        mock_plex.get_movies.return_value = [
            {"id": "m1", "title": "A", "year": 2020, "file_path": "/m/m1.mkv", "original_size": 1000}
        ]
        mock_jf = MagicMock()
        mock_jf.server_type = "jellyfin"
        mock_jf.config = {"libraries": ["Movies"]}
        mock_jf.get_movies.return_value = []
        specter.servers = [mock_plex, mock_jf]

        import mediaspecter
        old = mediaspecter.GLOBAL_SPECTER
        mediaspecter.GLOBAL_SPECTER = specter
        try:
            from fastapi.testclient import TestClient
            from mediaspecter import app
            client = TestClient(app)
            resp = client.get("/api/movies")
            movies = resp.json()
            if movies:
                self.assertEqual(movies[0]["missing_servers"], [])
        finally:
            mediaspecter.GLOBAL_SPECTER = old

    def test_archive_item_uses_manual_match(self):
        db = self.specter.db
        self.specter.config.setdefault("safety", {})["dry_run"] = False
        self.specter.config.setdefault("safety", {})["allow_automated_archival"] = True

        mock_plex = MagicMock()
        mock_plex.server_type = "plex"
        mock_plex.config = {"libraries": ["Movies"]}
        mock_plex.get_item_metadata.return_value = {
            "id": "m1", "title": "A", "type": "movie", "file_path": "/m/m1.mkv",
            "original_size": 1000, "external_ids": {}
        }
        mock_jf = MagicMock()
        mock_jf.server_type = "jellyfin"
        mock_jf.config = {"libraries": ["Movies"]}
        mock_jf.find_item.return_value = None  # auto-match fails
        mock_jf.get_item_metadata.return_value = {
            "id": "m99", "title": "A", "type": "movie", "file_path": "/m/m99.mkv",
            "original_size": 1000
        }
        mock_jf.download_poster.return_value = True
        mock_jf.upload_poster.return_value = True
        self.specter.servers = [mock_plex, mock_jf]

        db.link_manual_match("movie", [("plex", "m1", "A"), ("jellyfin", "m99", "B")])

        self.specter.overlay.apply_overlay = MagicMock(return_value=True)
        with patch("mediaspecter.os.path.exists", return_value=True), \
             patch("mediaspecter.shutil.copy2"), \
             patch("mediaspecter.DUMMY_VIDEOS", {".mkv": base64.b64encode(b"d" * 100).decode()}), \
             patch("mediaspecter.MediaSpecter._expand_external_ids", return_value={}), \
             patch("mediaspecter.MediaSpecter._replace_with_dummy", return_value=None), \
             patch("mediaspecter.MediaSpecter._new_poster_tmp", return_value="/tmp/test_poster.jpg"):
            res = self.specter.archive_item("plex", "m1")
            self.assertTrue(res.get("success"))
            mock_jf.get_item_metadata.assert_called_with("m99")


if __name__ == "__main__":
    unittest.main()
