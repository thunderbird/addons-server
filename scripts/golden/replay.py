#!/usr/bin/env python3
"""
Replay the golden sample against two deployments and record the responses.

Every URL in scripts/golden/samples/<endpoint>.tsv is fetched once from
target A and once from target B, GET only, redirects not followed. Status,
content type, Location and body are appended to a gzipped JSON-lines file
that compare.py turns into a diff report, so the report can be recomputed
and tuned without sending the requests again.

Production safety (not configurable downwards):
    - Any host that does not look like a stage/dev/local host is treated as
      production and capped at 2 requests per second.
    - The run stops at the first 5xx from a production host, and after 5
      consecutive transport errors against one.
    Use --resume to continue an interrupted run into the same output file.

Standard library only. Example (the first run, prod vs Fargate stage):
    python3 scripts/golden/replay.py --out prod-vs-stage.jsonl.gz

See scripts/golden/README.md.
"""
import argparse
import functools
import gzip
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, 'samples')

# endpoint -> which base URL (versioncheck or services) serves it
ENDPOINT_SERVICE = {
    'versioncheck': 'versioncheck',
    'api-v4': 'services',
    'api-v3': 'services',
}

DEFAULTS = {
    'a': {
        'versioncheck': 'https://versioncheck.addons.thunderbird.net',
        'services': 'https://services.addons.thunderbird.net',
    },
    'b': {
        'versioncheck': 'https://versioncheck.addons-stage.thunderbird.net',
        'services': 'https://addons-stage.thunderbird.net',
    },
}

# Hosts whose certificate is issued for another name. The chain is still
# verified; only the name checked against the certificate changes. The
# Fargate stage versioncheck ALB serves the *.addons.thunderbird.net cert.
DEFAULT_TLS_NAMES = {
    'versioncheck.addons-stage.thunderbird.net':
        'versioncheck.addons.thunderbird.net',
}

PROD_MAX_RPS = 2.0
PROD_MAX_TRANSPORT_ERRORS = 5
NON_PROD_MARKERS = ('-stage.', 'stage.', '-dev.', 'dev.', 'localhost',
                    '127.0.0.1', '.internal', '.local')
USER_AGENT = ('atn-golden-harness/1.0 '
              '(+https://github.com/thunderbird/addons-server/issues/398)')


def is_production(base):
    host = (urlsplit(base).hostname or '').lower()
    return not any(marker in host for marker in NON_PROD_MARKERS)


class RateLimiter:
    def __init__(self, rps):
        self.interval = 1.0 / rps
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = self.next_at
            self.next_at = now + self.interval


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TLSNameConnection(http.client.HTTPSConnection):
    """HTTPS connection that sends SNI for, and verifies, a declared name."""

    def __init__(self, host, tls_name=None, **kwargs):
        super().__init__(host, **kwargs)
        self.tls_name = tls_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(
            sock, server_hostname=self.tls_name or self.host)


def verified_context():
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class TLSNameHandler(urllib.request.HTTPSHandler):
    def __init__(self, tls_names):
        super().__init__(context=verified_context())
        self.tls_names = tls_names

    def https_open(self, req):
        conn = functools.partial(
            TLSNameConnection, tls_name=self.tls_names.get(req.host))
        return self.do_open(conn, req, context=self._context)


class Target:
    def __init__(self, label, bases, tls_names, rps, timeout):
        self.label = label
        self.bases = bases
        self.timeout = timeout
        self.limiters = {}
        self.prod = {}
        for service, base in bases.items():
            prod = is_production(base)
            self.prod[service] = prod
            self.limiters[service] = RateLimiter(
                min(rps, PROD_MAX_RPS) if prod else rps)
        self.opener = urllib.request.build_opener(
            NoRedirect, TLSNameHandler(tls_names))

    def fetch(self, service, url):
        self.limiters[service].wait()
        request = urllib.request.Request(
            self.bases[service] + url, method='GET',
            headers={'User-Agent': USER_AGENT,
                     'Accept-Encoding': 'gzip, deflate'})
        started = time.monotonic()
        try:
            resp = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as err:
            resp = err
        except Exception as err:  # transport errors are recorded, not raised
            return {'error': '%s: %s' % (type(err).__name__, err)}
        try:
            raw = resp.read()
        except Exception as err:
            return {'error': '%s: %s' % (type(err).__name__, err)}
        finally:
            resp.close()
        headers = resp.headers
        encoding = (headers.get('Content-Encoding') or '').lower()
        if encoding == 'gzip':
            raw = gzip.decompress(raw)
        elif encoding == 'deflate':
            raw = zlib.decompress(raw)
        try:
            body, body_encoding = raw.decode('utf-8'), 'utf-8'
        except UnicodeDecodeError:
            body, body_encoding = raw.hex(), 'hex'
        return {
            'status': resp.status if hasattr(resp, 'status') else resp.code,
            'content_type': headers.get('Content-Type'),
            'location': headers.get('Location'),
            'body': body,
            'body_encoding': body_encoding,
            'elapsed': round(time.monotonic() - started, 3),
        }


def safe_path(path):
    """Resolve a path given on the command line; it must be under the cwd."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(os.getcwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        sys.exit('%s is outside the current directory' % path)
    return resolved


def load_sample(samples, endpoint, limit):
    path = os.path.join(samples, '%s.tsv' % endpoint)
    rows = []
    with open(path) as fh:
        for line in fh:
            if line.startswith('#') or not line.strip():
                continue
            hits, url = line.rstrip('\n').split('\t', 1)
            rows.append((int(hits), url))
    return rows[:limit] if limit else rows


def done_keys(path):
    keys = set()
    if not os.path.exists(path):
        return keys
    with gzip.open(path, 'rt') as fh:
        try:
            for line in fh:
                rec = json.loads(line)
                keys.add((rec['endpoint'], rec['url']))
        except (EOFError, ValueError):
            pass  # a truncated final record is simply fetched again
    return keys


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    for side in ('a', 'b'):
        for service in ('versioncheck', 'services'):
            parser.add_argument(
                '--%s-%s' % (side, service),
                default=DEFAULTS[side][service],
                help='default %s' % DEFAULTS[side][service])
    parser.add_argument('--tls-name', action='append', default=[],
                        metavar='HOST=NAME',
                        help='verify HOST against the certificate name NAME '
                        '(repeatable; adds to %s)' % ', '.join(
                            '%s=%s' % kv for kv in DEFAULT_TLS_NAMES.items()))
    parser.add_argument('--samples', help='directory holding <endpoint>.tsv '
                        '(default: the committed samples)')
    parser.add_argument('--endpoints', default=','.join(ENDPOINT_SERVICE))
    parser.add_argument('--limit', type=int, default=0,
                        help='at most N urls per endpoint (0 = all)')
    parser.add_argument('--rps', type=float, default=5.0,
                        help='per-host rate for non-production hosts; '
                        'production is always capped at %s' % PROD_MAX_RPS)
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--out', required=True,
                        help='output .jsonl.gz under the current directory '
                        '(appended with --resume)')
    parser.add_argument('--resume', action='store_true')
    return parser.parse_args()


def build_targets(args):
    tls_names = dict(DEFAULT_TLS_NAMES)
    for item in args.tls_name:
        host, _, name = item.partition('=')
        tls_names[host] = name
    targets = {}
    for side in ('a', 'b'):
        bases = {s: getattr(args, '%s_%s' % (side, s)).rstrip('/')
                 for s in ('versioncheck', 'services')}
        targets[side] = Target(side.upper(), bases, tls_names, args.rps,
                               args.timeout)
        for service, base in bases.items():
            note = ('  [production: <= %s rps, stop on 5xx]' % PROD_MAX_RPS
                    if targets[side].prod[service] else '')
            print('target %s %-12s %s%s' % (side.upper(), service, base,
                                            note), file=sys.stderr)
    return targets


class ProductionGuard:
    """Stops the run on a production 5xx or repeated transport errors."""

    def __init__(self, targets):
        self.targets = targets
        self.transport_errors = dict.fromkeys(targets, 0)

    def check(self, service, rec):
        """Return a reason to stop, or None to carry on."""
        for side, target in self.targets.items():
            if not target.prod[service]:
                continue
            result = rec[side]
            if 'error' not in result:
                self.transport_errors[side] = 0
                if result['status'] >= 500:
                    return ('production target %s returned %d for %s'
                            % (target.label, result['status'], rec['url']))
                continue
            self.transport_errors[side] += 1
            if self.transport_errors[side] >= PROD_MAX_TRANSPORT_ERRORS:
                return ('%d consecutive transport errors from production '
                        'target %s (last: %s)' % (
                            self.transport_errors[side], target.label,
                            result['error']))
        return None


def replay_endpoint(endpoint, rows, targets, guard, out):
    service = ENDPOINT_SERVICE[endpoint]
    for i, (hits, url) in enumerate(rows, 1):
        rec = {'endpoint': endpoint, 'url': url, 'hits': hits}
        for side, target in targets.items():
            rec[side] = target.fetch(service, url)
        out.write(json.dumps(rec, sort_keys=True) + '\n')
        reason = guard.check(service, rec)
        if reason:
            out.flush()
            sys.exit('STOP: ' + reason)
        if i % 250 == 0:
            out.flush()
            print('  %s %d/%d' % (endpoint, i, len(rows)), file=sys.stderr)


def main():
    args = parse_args()
    samples = safe_path(args.samples) if args.samples else SAMPLES
    out_path = safe_path(args.out)
    targets = build_targets(args)
    if os.path.exists(out_path) and not args.resume:
        sys.exit('%s exists; pass --resume to continue it' % out_path)
    skip = done_keys(out_path) if args.resume else set()
    guard = ProductionGuard(targets)
    endpoints = [e.strip() for e in args.endpoints.split(',') if e.strip()]
    with gzip.open(out_path, 'at') as out:
        for endpoint in endpoints:
            rows = load_sample(samples, endpoint, args.limit)
            todo = [r for r in rows if (endpoint, r[1]) not in skip]
            print('%s: %d urls (%d already done)' % (
                endpoint, len(todo), len(rows) - len(todo)), file=sys.stderr)
            replay_endpoint(endpoint, todo, targets, guard, out)


if __name__ == '__main__':
    main()
