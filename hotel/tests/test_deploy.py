"""Deployment helpers: health probe, scheduler endpoints, DATABASE_URL parsing."""
from django.test import TestCase, override_settings

from hms.settings import _database_from_url

from .base import NO_LIMITS


@NO_LIMITS
class CronTests(TestCase):
    def test_healthz(self):
        r = self.client.get("/healthz/")
        self.assertEqual((r.status_code, r.content), (200, b"ok"))

    @override_settings(CRON_SECRET="")
    def test_disabled_without_secret(self):
        self.assertEqual(self.client.get("/api/cron/holds/?key=").status_code, 404)

    @override_settings(CRON_SECRET="s3cret")
    def test_requires_key(self):
        self.assertEqual(self.client.get("/api/cron/holds/").status_code, 403)
        self.assertEqual(self.client.get("/api/cron/holds/?key=nope").status_code, 403)
        self.assertEqual(self.client.get("/api/cron/holds/?key=s3cret").json()["ok"], True)
        self.assertEqual(self.client.get("/api/cron/holds/", HTTP_AUTHORIZATION="Bearer s3cret").status_code, 200)
        self.assertEqual(self.client.get("/api/cron/unknown/?key=s3cret").status_code, 404)


class DatabaseUrlTests(TestCase):
    def test_parse_neon_style_url(self):
        d = _database_from_url("postgresql://me:p%40ss@ep-1-pooler.aws.neon.tech:5432/neondb?sslmode=require&channel_binding=require")
        self.assertEqual((d["USER"], d["PASSWORD"], d["HOST"], d["NAME"], d["PORT"]), ("me", "p@ss", "ep-1-pooler.aws.neon.tech", "neondb", "5432"))
        self.assertEqual(d["OPTIONS"], {"sslmode": "require"})
        self.assertTrue(d["DISABLE_SERVER_SIDE_CURSORS"])
