# -*- coding: utf-8 -*-
"""
Read-only health check for ECS deployment validation

Validates that the Django application can boot, connect to all backend
services, and execute read-only operations in the ECS Fargate environment.
This sits between the infrastructure smoke test (TCP connectivity) and
running the full application (read-only and write operations)

What this checks
    - Container has correct Python path, deps, and module loading
    - Django settings import works end-to-end (including Secrets Manager)
    - ORM can connect to MySQL and execute SELECT queries
    - Cache backend (Redis/Memcached) initialises and responds
    - Celery broker (RabbitMQ) is reachable
    - Elasticsearch/OpenSearch client can connect

What this does NOT do:
    - Write to any database, cache, queue, or search index
    - Modify any state anywhere
    - Run migrations

Sample Usage:
    python manage.py ro_healthcheck          # Run all checks
    python manage.py ro_healthcheck --json   # Output as JSON
"""

import json as json_module
import sys
import time

from django.core.management.base import BaseCommand
from django.conf import settings

import olympia.core.logger

log = olympia.core.logger.getLogger("z.ro_healthcheck")


class Command(BaseCommand):
    help = "Read-only health check for ECS deployment validation"

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output results as JSON",
        )

    def handle(self, *args, **options):
        results = {}
        output_json = options.get("json", False)

        # -----------------------------------------------------------------
        # 1. Django settings loaded (if we got here, this already passed)
        # -----------------------------------------------------------------
        results["settings"] = {
            "description": "Django settings import",
            "status": "PASS",
            "message": f"DJANGO_SETTINGS_MODULE={settings.SETTINGS_MODULE}",
        }

        # -----------------------------------------------------------------
        # 2. Database connectivity (read-only)
        # -----------------------------------------------------------------
        results["database"] = self._check_database()

        # -----------------------------------------------------------------
        # 3. Cache backend
        # -----------------------------------------------------------------
        results["cache"] = self._check_cache()

        # -----------------------------------------------------------------
        # 4. Celery broker (RabbitMQ)
        # -----------------------------------------------------------------
        results["celery_broker"] = self._check_celery_broker()

        # -----------------------------------------------------------------
        # 5. Elasticsearch / OpenSearch
        # -----------------------------------------------------------------
        results["elasticsearch"] = self._check_elasticsearch()

        # -----------------------------------------------------------------
        # Output
        # -----------------------------------------------------------------
        if output_json:
            self.stdout.write(json_module.dumps(results, indent=2))
        else:
            self._print_results(results)

        has_failures = any(r["status"] == "FAIL" for r in results.values())
        if has_failures:
            sys.exit(1)

    def _check_database(self):
        """Connect to MySQL and run a read-only query via ORM.

        Sets session to transaction_read_only BEFORE any ORM work,
        and fails fast if read-only mode cannot just be enforced
        """
        try:
            from django.db import connections

            start = time.time()
            conn = connections["default"]
            conn.ensure_connection()
            cursor = conn.cursor()

            # Force read-only session BEFORE any ORM work
            cursor.execute("SET SESSION transaction_read_only = 1;")

            # Verify read-only mode is actually active
            cursor.execute("SELECT @@session.transaction_read_only;")
            ro_flag = cursor.fetchone()[0]
            if ro_flag != 1:
                cursor.close()
                return {
                    "description": "MySQL database (read-only enforcement)",
                    "status": "FAIL",
                    "message": "Could not enforce read-only session",
                }

            # Now safe to run ORM queries -- writes would be rejected by MySQL
            from olympia.addons.models import Addon

            count = Addon.objects.count()
            latency_ms = (time.time() - start) * 1000

            # Clean up
            cursor.execute("SET SESSION transaction_read_only = 0;")
            cursor.close()

            return {
                "description": "MySQL database (read-only ORM query)",
                "status": "PASS",
                "message": f"Connected, {count} addons ({latency_ms:.0f}ms)",
            }
        except Exception as e:
            # Include configured host for diagnostics
            try:
                db_host = settings.DATABASES.get("default", {}).get("HOST", "not set")
                db_engine = settings.DATABASES.get("default", {}).get("ENGINE", "not set")
                diag = f" [configured: engine={db_engine}, host={db_host}]"
            except Exception:
                diag = ""
            return {
                "description": "MySQL database (read-only ORM query)",
                "status": "FAIL",
                "message": f"{e}{diag}",
            }

    def _check_cache(self):
        """Verify Django cache backend can connect and respond"""
        try:
            from django.core.cache import cache

            start = time.time()

            # Use a harmless get (returns None if key doesn't exist)
            # This exercises the full cache client initialisation path
            cache.get("ro_healthcheck_probe")
            latency_ms = (time.time() - start) * 1000

            backend = settings.CACHES.get("default", {}).get(
                "BACKEND", "unknown"
            )

            return {
                "description": "Cache backend",
                "status": "PASS",
                "message": f"Backend: {backend} ({latency_ms:.0f}ms)",
            }
        except Exception as e:
            return {
                "description": "Cache backend",
                "status": "FAIL",
                "message": str(e),
            }

    def _check_celery_broker(self):
        """Verify Celery can connect to the broker (RabbitMQ).

        Uses ensure_connection with a short timeout
        """
        try:
            from olympia.amo.celery import app as celery_app

            start = time.time()
            conn = celery_app.connection()
            conn.ensure_connection(max_retries=1, timeout=5)
            conn.close()
            latency_ms = (time.time() - start) * 1000

            return {
                "description": "Celery broker (RabbitMQ)",
                "status": "PASS",
                "message": f"Connected ({latency_ms:.0f}ms)",
            }
        except Exception as e:
            # Strip any connection details from the error
            error_msg = str(e).split("@")[-1] if "@" in str(e) else str(e)
            return {
                "description": "Celery broker (RabbitMQ)",
                "status": "FAIL",
                "message": error_msg,
            }

    def _check_elasticsearch(self):
        """Verify Elasticsearch/OpenSearch client can connect.

        Calls es.info() which is a read-only cluster metadata endpoint
        """
        try:
            # olympia.lib.es.utils provides helper functions for reindexing,
            # but the canonical ES client factory lives in olympia.amo.search
            # We'd import from there to avoid ImportError and ensure consistency
            from olympia.amo.search import get_es

            start = time.time()
            es = get_es()
            info = es.info(request_timeout=5)
            latency_ms = (time.time() - start) * 1000

            version = info.get("version", {}).get("number", "unknown")

            return {
                "description": "Elasticsearch / OpenSearch",
                "status": "PASS",
                "message": f"Reachable, version: {version} ({latency_ms:.0f}ms)",
            }
        except Exception as e:
            # ES may require SigV4 auth or be unavailable; degrade gracefully
            error_type = type(e).__name__
            return {
                "description": "Elasticsearch / OpenSearch",
                "status": "FAIL",
                "message": f"{error_type}: {e}",
            }

    def _print_results(self, results):
        """Print results in human-readable format"""
        self.stdout.write("")
        self.stdout.write("=" * 70)
        self.stdout.write("ATN Read-Only Health Check (ECS Deployment Validation)")
        self.stdout.write("=" * 70)

        passed = 0
        failed = 0

        for name, result in results.items():
            status = result["status"]
            icon = "[OK]" if status == "PASS" else "[FAIL]"

            if status == "PASS":
                passed += 1
            else:
                failed += 1

            self.stdout.write(f"\n  {icon} {result['description']}")
            self.stdout.write(f"       {result['message']}")

        self.stdout.write("")
        self.stdout.write("-" * 70)
        self.stdout.write(f"  Results: {passed} passed, {failed} failed")
        self.stdout.write("=" * 70)
        self.stdout.write("")
