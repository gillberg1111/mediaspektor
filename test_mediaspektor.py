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

    def test_trigger_spektor_accepts_numeric_item_id(self):
        # Plex ratingKeys arrive as JSON numbers; must not 422 (regression).
        with patch.object(self.spektor, "archive_item") as mock_archive:
            resp = self.client.post("/api/spektor", json={"server_type": "plex", "item_id": 12345})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["success"], True)

    def test_poster_proxy(self):
        fake_item = MagicMock()
        fake_item.posterUrl = "http://mock-plex/poster.jpg"
        self.mock_connector._server.fetchItem.return_value = fake_item

        fake_resp = MagicMock()
        fake_resp.iter_content = lambda chunk_size=1024: iter([b"fake-image-data"])
        fake_resp.raise_for_status = lambda: None
        fake_resp.headers = {"Content-Type": "image/jpeg"}

        with patch("mediaspektor.requests.get", return_value=fake_resp):
            resp = self.client.get("/api/posterproxy?server_type=plex&item_id=1")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Cache-Control"], "public, max-age=86400")
        self.mock_connector._server.fetchItem.assert_called_with(1)


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

    def _make_spektor(self):
        from mediaspektor import MediaSpektor
        import yaml
        with open(self.temp_config.name, "w") as f:
            yaml.safe_dump(self.config_data, f)
        spektor = MediaSpektor(self.temp_config.name)
        spektor.db = Database(self.temp_db.name)
        return spektor

    def _make_spektor_with_mock_servers(self, *servers):
        """Create spktor and replace servers with mock list."""
        spektor = self._make_spektor()
        spektor.servers = list(servers)
        return spektor

    @patch("mediaspektor.PlexServer")
    @patch("mediaspektor.JellyfinConnector.authenticate")
    def test_find_item_path_then_id(self, mock_jf_auth, mock_plex_server_cls):
        # Build Plex connector with mocked section/search
        mock_plex = MagicMock()
        mock_plex_server_cls.return_value = mock_plex

        import mediaspektor
        plex_cfg = {"type": "plex", "enabled": True, "url": "http://mock-plex", "token": "t", "libraries": ["Movies"]}
        plex = mediaspektor.PlexConnector(plex_cfg)

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

    @patch("mediaspektor.JellyfinConnector.authenticate")
    @patch("mediaspektor.PlexServer")
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

        spektor = self._make_spektor_with_mock_servers(plex, jf)

        # The real filesystem swap is covered by TestSafeReplace; here we stub it
        # so propagation logic can run against fake paths.
        with patch.object(MediaSpektor, "_replace_with_dummy", return_value=None), \
             patch("mediaspektor.shutil.copy2"), \
             patch("builtins.open"):
            result = spektor.archive_item("plex", "1")

        self.assertTrue(result["success"])
        jf.find_item.assert_called()
        self.assertGreaterEqual(plex.upload_poster.call_count, 1)
        self.assertGreaterEqual(jf.upload_poster.call_count, 1)
        self.assertEqual(spektor.db.get_stats()["total_items"], 2)

    @patch("mediaspektor.JellyfinConnector.authenticate")
    @patch("mediaspektor.PlexServer")
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

        spektor = self._make_spektor_with_mock_servers(plex, jf)

        # The real filesystem swap is covered by TestSafeReplace; here we stub it
        # so propagation logic can run against fake paths.
        with patch.object(MediaSpektor, "_replace_with_dummy", return_value=None), \
             patch("mediaspektor.shutil.copy2"), \
             patch("builtins.open"):
            result = spektor.archive_item("plex", "1")

        self.assertTrue(result["success"])
        self.assertTrue(any("jellyfin" in str(w).lower() for w in result.get("warnings", [])))
        self.assertEqual(spektor.db.get_stats()["total_items"], 1)
        jf.upload_poster.assert_not_called()

    def test_restore_fans_out(self):
        spektor = self._make_spektor()

        # Seed two sibling rows
        spektor.db.insert(
            server_type="plex", server_item_id="1", title="Movie", media_type="movie",
            original_path="/data/Movie.mp4", original_size_bytes=100, dummy_size_bytes=10,
            backup_poster_path="/backups/plex_1_poster_original.jpg", status="archived",
        )
        spektor.db.insert(
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

        spektor.servers = [plex, jf]

        with patch("os.path.exists", return_value=True), patch("shutil.move"):
            self.assertTrue(spektor.restore("plex", "1"))

        # Both rows restored
        self.assertEqual(spektor.db.get_item("plex", "1")["status"], "restored")
        self.assertEqual(spektor.db.get_item("jellyfin", "99")["status"], "restored")
        # Upload called for both
        plex.upload_poster.assert_called()
        jf.upload_poster.assert_called()

    def test_expand_external_ids_bridges(self):
        from mediaspektor import TmdbClient

        tmdb = TmdbClient("fake-key-not-jwt")

        def fake_get(path, **params):
            if "/find/" in path:
                return {"movie_results": [{"id": 99}]}
            if "/external_ids" in path:
                return {"imdb_id": "tt789", "tvdb_id": 2020}
            return {}

        tmdb._get = fake_get

        spektor = self._make_spektor()
        spektor.tmdb = tmdb

        # Expand from imdb only
        result = spektor._expand_external_ids("movie", {"tmdb": None, "imdb": "tt123", "tvdb": None})
        self.assertEqual(result["tmdb"], "99")
        self.assertEqual(result["imdb"], "tt123")
        self.assertEqual(result["tvdb"], "2020")

        # Episodes return input unchanged
        ep_result = spektor._expand_external_ids("episode", {"tmdb": None, "imdb": "tt123"})
        self.assertEqual(ep_result["tmdb"], None)

        # When disabled, return input unchanged
        tmdb.api_key = ""
        result3 = spektor._expand_external_ids("movie", {"tmdb": None, "imdb": "tt123", "tvdb": None})
        self.assertEqual(result3["tmdb"], None)


class TestSafeReplace(unittest.TestCase):
    """Guards the data-safety contract of _replace_with_dummy."""

    def _spektor(self):
        # Bare instance — _replace_with_dummy needs no config/connectors.
        return MediaSpektor.__new__(MediaSpektor)

    def test_atomic_replace(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "movie.mkv")
            with open(f, "wb") as fh:
                fh.write(b"X" * 1000)
            out = self._spektor()._replace_with_dummy(f, b"DUMMY")
            self.assertIsNone(out)
            with open(f, "rb") as fh:
                self.assertEqual(fh.read(), b"DUMMY")

    def test_replace_with_backup_preserves_original(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "movie.mkv")
            backup = os.path.join(d, "plex_1.mkv")
            with open(f, "wb") as fh:
                fh.write(b"ORIGINAL")
            out = self._spektor()._replace_with_dummy(f, b"DUMMY", backup)
            self.assertEqual(out, backup)
            with open(f, "rb") as fh:
                self.assertEqual(fh.read(), b"DUMMY")
            with open(backup, "rb") as fh:
                self.assertEqual(fh.read(), b"ORIGINAL")

    def test_missing_file_raises_without_side_effects(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "does-not-exist.mkv")
            with self.assertRaises(FileNotFoundError):
                self._spektor()._replace_with_dummy(f, b"DUMMY")
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
            self._spektor()._replace_with_dummy(f, b"DUMMY")
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
