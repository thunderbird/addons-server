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
        """Connect to MySQL and run a read-only query via ORM"""
        try:
            from django.db import connections

            start = time.time()
            conn = connections["default"]
            cursor = conn.cursor()

            # Force read-only session to guarantee no writes
            cursor.execute("SET SESSION transaction_read_only = 1;")

            # Run a real ORM-level query: count addons
            from olympia.addons.models import Addon

            count = Addon.objects.count()
            latency_ms = (time.time() - start) * 1000

            # Reset session
            cursor.execute("SET SESSION transaction_read_only = 0;")
            cursor.close()

            return {
                "description": "MySQL database (read-only ORM query)",
                "status": "PASS",
                "message": f"Connected, {count} addons in DB ({latency_ms:.0f}ms)",
            }
        except Exception as e:
            return {
                "description": "MySQL database (read-only ORM query)",
                "status": "FAIL",
                "message": str(e),
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
        """Verify Celery can connect to the broker (RabbitMQ)"""
        try:
            from olympia.amo.celery import app as celery_app

            start = time.time()
            conn = celery_app.connection()
            conn.ensure_connection(max_retries=1, timeout=5)
            conn.close()
            latency_ms = (time.time() - start) * 1000

            broker_url = celery_app.conf.broker_url or "not configured"
            # Mask credentials if present
            if "@" in str(broker_url):
                broker_display = (
                    broker_url.split("@")[-1] if "@" in str(broker_url) else broker_url
                )
            else:
                broker_display = broker_url

            return {
                "description": "Celery broker (RabbitMQ)",
                "status": "PASS",
                "message": f"Connected to {broker_display} ({latency_ms:.0f}ms)",
            }
        except Exception as e:
            return {
                "description": "Celery broker (RabbitMQ)",
                "status": "FAIL",
                "message": str(e),
            }

    def _check_elasticsearch(self):
        """Verify Elasticsearch/OpenSearch client can connect"""
        try:
            from olympia.lib.es.utils import get_es

            start = time.time()
            es = get_es()
            info = es.info()
            latency_ms = (time.time() - start) * 1000

            version = info.get("version", {}).get("number", "unknown")
            cluster = info.get("cluster_name", "unknown")

            return {
                "description": "Elasticsearch / OpenSearch",
                "status": "PASS",
                "message": f"Cluster: {cluster}, version: {version} ({latency_ms:.0f}ms)",
            }
        except Exception as e:
            # ES may require SigV4 auth or be unavailable; degrade gracefully
            return {
                "description": "Elasticsearch / OpenSearch",
                "status": "FAIL",
                "message": str(e),
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
