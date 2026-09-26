#!/usr/bin/env python3
"""
Build the golden-response request sample from CloudFront standard logs.

Reads the CloudFront access logs for the Thunderbird client endpoints and
writes a deterministic sample of distinct request URLs (path plus query
string) to scripts/golden/samples/. Only the path, the query string and a hit
count are kept: client IPs, user agents, cookies and every other log field
are dropped at parse time and never written anywhere.

Sources (thunderbird-legacy account, read-only):
    versioncheck  s3://versioncheck-logs/E1PQMC7BGJOP5E.*          (E1PQMC7BGJOP5E)
    services      s3://services-addons-logs/services-logs/EGX44RIFURRUW.*

Determinism:
    - The window is the N full UTC days before --end (default: today, UTC).
    - Within each hour, the log files with the smallest sha256(key) are read,
      --files-per-hour of them.
    - Distinct URLs are sampled bottom-k by sha256(url), so the same logs
      always give the same sample regardless of read order.
    The logs expire after 90 days, so the committed sample files are the
    reproducibility anchor; manifest.json records exactly what was read.

Requires the aws CLI and a profile that can read the log buckets:
    python3 scripts/golden/sample.py --profile mzla-tb-legacy

See scripts/golden/README.md.
"""
import argparse
import collections
import datetime
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import functools
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, 'samples')

SOURCES = {
    'versioncheck': {
        'bucket': 'versioncheck-logs',
        'prefix': '',
        'distribution': 'E1PQMC7BGJOP5E',
    },
    'services': {
        'bucket': 'services-addons-logs',
        'prefix': 'services-logs/',
        'distribution': 'EGX44RIFURRUW',
    },
}

# endpoint name -> (log source, path matcher, default sample size)
ENDPOINTS = {
    'versioncheck': (
        'versioncheck',
        lambda path: path == '/update/VersionCheck.php',
        10000,
    ),
    'api-v4': ('services', lambda path: path.startswith('/api/v4/'), 2000),
    'api-v3': ('services', lambda path: path.startswith('/api/v3/'), 2000),
}

# A request carrying any of these parameters is dropped whole rather than
# edited, so nothing that affects a response is ever stripped.
SENSITIVE_PARAMS = {
    'access_token', 'auth', 'code', 'email', 'jwt', 'password', 'session',
    'sessionid', 'sig', 'signature', 'token',
}

# Self-distributed add-ons sometimes use the author's personal address as
# their id. Requests naming such an id are dropped so the public sample does
# not carry anyone's email address.
MAIL_PROVIDERS = (
    'gmail googlemail yahoo ymail rocketmail hotmail outlook live msn '
    'passport icloud me mac aol aim gmx web mail email inbox list bk rambler '
    'yandex ya ukr bigmir proton protonmail pm posteo mailbox tutanota tuta '
    'disroot riseup fastmail zoho hushmail qq foxmail 163 126 yeah sina '
    'sohu aliyun naver daum hanmail nate rediffmail free orange wanadoo '
    'laposte sfr neuf club-internet t-online freenet arcor seznam centrum '
    'wp o2 onet interia libero virgilio tiscali alice tin telenet skynet '
    'ziggo xs4all planet home kpnmail bluewin hispeed chello aon comcast '
    'verizon att sbcglobal bellsouth cox charter earthlink juno shaw rogers '
    'sympatico videotron bigpond optusnet iinet btinternet btopenworld sky '
    'ntlworld blueyonder talktalk terra uol bol ig abv thundermail'
).split()
MAIL_DOMAINS = ('i.ua meta.ua mail.bg tb.pro mozmail.com relay.firefox.com '
                'duck.com simplelogin.com anonaddy.me addy.io').split()
PERSONAL_ADDRESS = re.compile(
    r'@((%s)\.[a-z.]{2,}|(%s)\b)' % (
        '|'.join(map(re.escape, MAIL_PROVIDERS)),
        '|'.join(map(re.escape, MAIL_DOMAINS))),
    re.IGNORECASE)

# CloudFront standard log (v1.0) field positions.
F_METHOD, F_STEM, F_QUERY = 5, 7, 11


def aws(profile, *args, binary=False):
    cmd = ['aws', '--profile', profile] + list(args)
    out = subprocess.run(
        cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout
    return out if binary else out.decode('utf-8')


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def list_hour(profile, source, hour):
    prefix = '%s%s.%s' % (
        source['prefix'], source['distribution'], hour.strftime('%Y-%m-%d-%H')
    )
    out = aws(
        profile, 's3api', 'list-objects-v2', '--bucket', source['bucket'],
        '--prefix', prefix, '--query', 'Contents[].Key', '--output', 'json',
    )
    return json.loads(out) or []


def pick_keys(profile, source, hours, files_per_hour, workers):
    with ThreadPoolExecutor(workers) as pool:
        listings = list(
            pool.map(lambda h: list_hour(profile, source, h), hours)
        )
    keys = []
    for hour, hour_keys in zip(hours, listings):
        if not hour_keys:
            print('  no logs for %s' % hour.isoformat(), file=sys.stderr)
        keys.extend(sorted(hour_keys, key=sha)[:files_per_hour])
    return keys


def url_from_log(fields):
    """Rebuild the request URL as the client sent it.

    CloudFront percent-encodes the path and the query once more when logging
    (a literal '%' is logged as %25), so a single unquote gives back the
    original.
    """
    query = fields[F_QUERY]
    url = unquote(fields[F_STEM])
    if query and query != '-':
        url += '?' + unquote(query)
    return url


def is_sensitive(url):
    if PERSONAL_ADDRESS.search(unquote(url)):
        return True
    if '?' not in url:
        return False
    names = {k.lower() for k, _ in parse_qsl(url.split('?', 1)[1], True)}
    return bool(names & SENSITIVE_PARAMS)


class BottomK:
    """Keep the k URLs with the smallest hashes, with exact hit counts.

    The admission threshold only ever falls, so any URL in the final set was
    below it from its first occurrence and its count is exact.
    """

    def __init__(self, k):
        self.k = k
        self.items = {}
        self.threshold = None

    def add(self, url, count=1):
        h = sha(url)
        if self.threshold is not None and h > self.threshold:
            return
        entry = self.items.get(h)
        if entry:
            entry[0] += count
            return
        self.items[h] = [count, url]
        if len(self.items) > 2 * self.k:
            self._prune()

    def _prune(self):
        keep = sorted(self.items)[: self.k]
        self.items = {h: self.items[h] for h in keep}
        if len(keep) == self.k:
            self.threshold = keep[-1]

    def result(self):
        self._prune()
        return [(h, c, u) for h, (c, u) in sorted(self.items.items())]


def scan_key(profile, bucket, key, matchers):
    """Return {endpoint: Counter(url)} for GET requests in one log file."""
    raw = aws(profile, 's3', 'cp', 's3://%s/%s' % (bucket, key), '-',
              binary=True)
    found = {name: collections.Counter() for name in matchers}
    for line in gzip.decompress(raw).decode('utf-8', 'replace').splitlines():
        if line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) <= F_QUERY or fields[F_METHOD] != 'GET':
            continue
        for name, match in matchers.items():
            if match(fields[F_STEM]):
                url = url_from_log(fields)
                # A decoded control character would break the TSV sample.
                if not any(c in url for c in '\t\r\n'):
                    found[name][url] += 1
    return found


class EndpointSample:
    """Counts and bottom-k sample for one endpoint."""

    def __init__(self, name):
        self.name = name
        self.sampler = BottomK(ENDPOINTS[name][2])
        self.requests = 0
        self.dropped = 0

    def add(self, urls):
        for url, count in urls.items():
            self.requests += count
            if is_sensitive(url):
                self.dropped += count
            else:
                self.sampler.add(url, count)

    def write(self, out):
        rows = self.sampler.result()
        path = os.path.join(out, '%s.tsv' % self.name)
        with open(path, 'w') as fh:
            fh.write('# hits\turl\n')
            for _, count, url in rows:
                fh.write('%d\t%s\n' % (count, url))
        print('%s: %d distinct urls from %d requests -> %s' % (
            self.name, len(rows), self.requests, path), file=sys.stderr)
        return {'requests': self.requests, 'dropped_sensitive': self.dropped,
                'distinct_sampled': len(rows),
                'target': ENDPOINTS[self.name][2]}


def scan_source(args, source, hours, samples):
    """Read one distribution's logs into the samples; return keys read."""
    matchers = {name: ENDPOINTS[name][1] for name in samples}
    keys = pick_keys(args.profile, source, hours, args.files_per_hour,
                     args.workers)
    print('reading %d files' % len(keys), file=sys.stderr)
    scan = functools.partial(scan_key, args.profile, source['bucket'],
                             matchers=matchers)
    with ThreadPoolExecutor(args.workers) as pool:
        for i, found in enumerate(pool.map(scan, keys), 1):
            for name, urls in found.items():
                samples[name].add(urls)
            if i % 50 == 0:
                print('  %d/%d files' % (i, len(keys)), file=sys.stderr)
    return keys


def safe_path(path):
    """Resolve a path given on the command line; it must be under the cwd."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(os.getcwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        sys.exit('%s is outside the current directory' % path)
    return resolved


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--profile', default='mzla-tb-legacy')
    parser.add_argument('--end', help='UTC date (YYYY-MM-DD), exclusive; '
                        'default today')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--files-per-hour', type=int, default=2)
    parser.add_argument('--endpoints', default=','.join(ENDPOINTS),
                        help='comma-separated subset of %s'
                        % ','.join(ENDPOINTS))
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--out', help='output directory under the current '
                        'directory (default: the committed samples)')
    return parser.parse_args()


def main():
    args = parse_args()
    out = safe_path(args.out) if args.out else DEFAULT_OUT
    if args.end:
        end = datetime.datetime.strptime(args.end, '%Y-%m-%d')
    else:
        end = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    start = end - datetime.timedelta(days=args.days)
    hours = [start + datetime.timedelta(hours=i)
             for i in range(args.days * 24)]
    wanted = [e.strip() for e in args.endpoints.split(',') if e.strip()]
    manifest = {
        'window_start': start.isoformat() + 'Z',
        'window_end': end.isoformat() + 'Z',
        'files_per_hour': args.files_per_hour,
        'sources': {},
        'endpoints': {},
    }

    for source_name, source in SOURCES.items():
        samples = {name: EndpointSample(name) for name in wanted
                   if ENDPOINTS[name][0] == source_name}
        if not samples:
            continue
        print('listing %s logs %s .. %s' % (source_name, start, end),
              file=sys.stderr)
        keys = scan_source(args, source, hours, samples)
        manifest['sources'][source_name] = dict(source, keys=keys)
        os.makedirs(out, exist_ok=True)
        for name, sample in samples.items():
            manifest['endpoints'][name] = sample.write(out)

    with open(os.path.join(out, 'manifest.json'), 'w') as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write('\n')


if __name__ == '__main__':
    main()
