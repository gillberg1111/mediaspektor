#!/usr/bin/env python3
"""Unit tests for MediaSpektor."""

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
from mediaspektor import (
    _parse_iso_date,
    Database,
    PosterOverlay,
    MediaSpektor,
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


class TestPosterOverlay(unittest.TestCase):
    def setUp(self):
        self.config = {
            "aesthetics": {
                "enable_poster_overlay": True,
                "banner_color": [20, 20, 20, 204],
                "border_color": [212, 175, 55, 255],
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
                self.assertEqual(out_img.format, "PNG")
        finally:
            if os.path.exists(input_path):
                os.unlink(input_path)
            if os.path.exists(output_path):
                os.unlink(output_path)


class TestOrchestratorFilter(unittest.TestCase):
    @patch("mediaspektor.Database")
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

        self.spektor = MediaSpektor(self.temp_config.name)

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

        filtered = self.spektor._filter_items(items)
        
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

    @patch("mediaspektor.Database")
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
            
        spektor = MediaSpektor(self.temp_config.name)
        spektor.db.item_exists.return_value = False
        
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
        
        spektor.servers = [mock_server]
        
        with patch("os.path.exists", return_value=True), \
             patch("os.path.splitext", return_value=("/path/movie", ".mp4")):
            results = spektor.archive(dry_run=False)
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


class TestFastAPI(unittest.TestCase):
    def setUp(self):
        # We can construct a mock MediaSpektor or a test MediaSpektor
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
            
        # Temporarily mock CONFIG_PATH and GLOBAL_SPEKTOR
        import mediaspektor
        self.old_config_path = mediaspektor.CONFIG_PATH
        self.old_global_spektor = mediaspektor.GLOBAL_SPEKTOR
        mediaspektor.CONFIG_PATH = self.temp_config.name
        
        # Patch the connectors so we don't try connecting to live server
        # when initializing MediaSpektor
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
        
        # Instantiate MediaSpektor and override components
        self.spektor = MediaSpektor(self.temp_config.name)
        self.spektor.db = Database(self.temp_db.name)
        self.spektor.servers = [self.mock_connector]
        
        mediaspektor.GLOBAL_SPEKTOR = self.spektor
        from fastapi.testclient import TestClient
        from mediaspektor import app
        self.client = TestClient(app)

    def tearDown(self):
        import mediaspektor
        mediaspektor.CONFIG_PATH = self.old_config_path
        mediaspektor.GLOBAL_SPEKTOR = self.old_global_spektor
        
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
        if os.path.exists(self.temp_config.name):
            os.unlink(self.temp_config.name)

    def test_read_root(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("MediaSpektor", resp.text)

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
        
        import mediaspektor
        # Verify it reloaded on GLOBAL_SPEKTOR
        self.assertEqual(mediaspektor.GLOBAL_SPEKTOR.config["rules"]["min_age_days"], 14)

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
        with patch.object(self.spektor, "archive_item") as mock_archive:
            resp = self.client.post("/api/spektor", json={"server_type": "plex", "item_id": "1"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["success"], True)
            
        with patch.object(self.spektor, "restore") as mock_restore:
            resp = self.client.post("/api/restore", json={"server_type": "plex", "item_id": "1"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["success"], True)


class TestAPISecurity(unittest.TestCase):
    def setUp(self):
        import mediaspektor
        self.old_config_path = mediaspektor.CONFIG_PATH
        self.old_global_spektor = mediaspektor.GLOBAL_SPEKTOR
        
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
            
        mediaspektor.CONFIG_PATH = self.temp_config.name
        self.spektor = MediaSpektor(self.temp_config.name)
        self.spektor.db = Database(self.temp_db.name)
        mediaspektor.GLOBAL_SPEKTOR = self.spektor
        
        from fastapi.testclient import TestClient
        from mediaspektor import app
        self.client = TestClient(app)

    def tearDown(self):
        import mediaspektor
        mediaspektor.CONFIG_PATH = self.old_config_path
        mediaspektor.GLOBAL_SPEKTOR = self.old_global_spektor
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


if __name__ == "__main__":
    unittest.main()
