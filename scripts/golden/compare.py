#!/usr/bin/env python3
"""
Classify the responses recorded by replay.py and write a diff report.

Each A/B pair is normalized, then compared by status, content type and body,
and every difference found is filed under a category with a kind:

    identical   equal after normalization
    data        same response shape, different content: values, list
                lengths, locale/guid map keys, null vs value, empty vs
                populated lists, 200 on one side and 404 on the other. The
                expected result of comparing against a DB copied from prod
                in 2024.
    behavioral  different shape or protocol: a field present on one side
                only, a scalar changing type, a status or content-type
                change, a redirect going somewhere else, a non-JSON body
                where JSON was expected. Each of these needs an explanation.
    error       a 5xx or a transport error on either side.

A pair's kind is the most severe of its findings (error > behavioral > data).

Normalization, applied to both sides before comparing:
    - site hostnames (--site-host, repeatable) are replaced with <site>, so
      addons-stage.thunderbird.net and services.addons.thunderbird.net links
      compare equal
    - ISO-8601 and HTTP-date timestamps become <timestamp>
    - 32-hex request ids become <request-id>
    - JSON object key order and XML attribute order are ignored
    - response headers other than Content-Type and Location are ignored

Example:
    python3 scripts/golden/compare.py prod-vs-stage.jsonl.gz \\
        --markdown report.md --json report.json

See scripts/golden/README.md.
"""
import argparse
import collections
import gzip
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'samples')

DEFAULT_SITE_HOSTS = [
    'versioncheck.addons-stage.thunderbird.net',
    'addons-stage.thunderbird.net',
    'versioncheck-bg.addons.thunderbird.net',
    'versioncheck.addons.thunderbird.net',
    'services.addons.thunderbird.net',
    'addons.thunderbird.net',
]

TIMESTAMP = re.compile(
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?'
    r'|\w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} GMT')
REQUEST_ID = re.compile(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])')

# Dict keys that are data rather than schema: locales, add-on guids, ids and
# application names (categories and compatibility are keyed by app).
LOCALE_KEY = re.compile(r'^[a-z]{2,3}([-_][A-Za-z0-9]{2,8})*$')
DATA_KEY = re.compile(r'[@{}]|^\d+$')
APP_KEYS = {'android', 'firefox', 'seamonkey', 'thunderbird'}
# Short schema field names that would otherwise pass as locale codes.
SCHEMA_WORDS = {'alt', 'app', 'id', 'is', 'key', 'max', 'min', 'src', 'tag',
                'url'}

# Fields the server includes or omits depending on the row, so presence on
# one side only is data drift. services/update.py: 'addons' is absent ({})
# when the add-on is unknown; the rest depend on strict_compat, the file hash
# and whether the version has release notes.
OPTIONAL_PATHS = {
    'addons',
    'addons{}.updates[].applications.gecko.strict_max_version',
    'addons{}.updates[].update_hash',
    'addons{}.updates[].update_info_url',
}
# The same optional fields in reqVersion=1 RDF responses, by element name.
OPTIONAL_XML_TAGS = {'updateHash', 'updateInfoURL'}

SEVERITY = {'identical': 0, 'data': 1, 'behavioral': 2, 'error': 3}


class Findings:
    def __init__(self):
        self.items = []

    def add(self, kind, category, path=''):
        self.items.append((kind, category, path))

    def kind(self):
        if not self.items:
            return 'identical'
        return max((k for k, _, _ in self.items), key=SEVERITY.get)


def scrub(text, site_hosts):
    for host in site_hosts:
        text = text.replace(host, '<site>')
    text = TIMESTAMP.sub('<timestamp>', text)
    return REQUEST_ID.sub('<request-id>', text)


def is_data_key(key):
    return (key in APP_KEYS or bool(DATA_KEY.search(key))
            or (bool(LOCALE_KEY.match(key)) and key not in SCHEMA_WORDS))


def is_data_map(a, b):
    keys = set(a) | set(b)
    return bool(keys) and all(is_data_key(k) for k in keys)


def type_name(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, (int, float)):
        return 'number'
    return type(value).__name__


def compare_values(a, b, path, findings):
    """Walk two parsed bodies, recording findings with generalized paths.

    List indices generalize to [] and data-map keys to {} so the report
    aggregates, e.g., results[].current_version.files[].hash.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        if is_data_map(a, b):
            compare_data_maps(a, b, path + '{}', findings)
        else:
            compare_objects(a, b, path, findings)
    elif isinstance(a, list) and isinstance(b, list):
        compare_lists(a, b, path + '[]', findings)
    else:
        compare_scalars(a, b, path, findings)


def compare_data_maps(a, b, path, findings):
    for key in set(a) | set(b):
        if key in a and key in b:
            compare_values(a[key], b[key], path, findings)
        else:
            findings.add('data', 'map key only in %s'
                         % ('A' if key in a else 'B'), path)


def compare_objects(a, b, path, findings):
    for key in sorted(set(a) | set(b)):
        sub = '%s.%s' % (path, key) if path else key
        if key in a and key in b:
            compare_values(a[key], b[key], sub, findings)
            continue
        side = 'A' if key in a else 'B'
        if sub in OPTIONAL_PATHS or key in OPTIONAL_XML_TAGS:
            findings.add('data', 'optional field only in %s' % side, sub)
        else:
            findings.add('behavioral', 'field only in %s' % side, sub)


def compare_lists(a, b, path, findings):
    if not a and b:
        findings.add('data', 'list empty in A', path)
    elif a and not b:
        findings.add('data', 'list empty in B', path)
    elif len(a) != len(b):
        findings.add('data', 'list length differs', path)
    for x, y in zip(a, b):
        compare_values(x, y, path, findings)


def compare_scalars(a, b, path, findings):
    ta, tb = type_name(a), type_name(b)
    if ta == tb:
        if a != b:
            findings.add('data', 'value differs', path)
    elif ta == 'null':
        findings.add('data', 'null in A', path)
    elif tb == 'null':
        findings.add('data', 'null in B', path)
    else:
        findings.add('behavioral', 'type %s -> %s' % (ta, tb), path)


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


def xml_to_obj(element):
    """Element tree to dicts, dropping namespace URIs from names."""
    obj = {'@' + local_name(k): v for k, v in element.attrib.items()}
    text = (element.text or '').strip()
    if text:
        obj['#text'] = text
    children = collections.OrderedDict()
    for child in element:
        children.setdefault(local_name(child.tag), []).append(
            xml_to_obj(child))
    obj.update(children)
    return obj


def parse_body(side, site_hosts):
    ctype = (side.get('content_type') or '').split(';')[0].strip().lower()
    body = scrub(side.get('body') or '', site_hosts)
    if side.get('body_encoding') == 'hex':
        return ctype, 'binary', body
    if 'json' in ctype:
        try:
            return ctype, 'json', json.loads(body)
        except ValueError:
            return ctype, 'invalid-json', body
    if 'xml' in ctype or body.lstrip().startswith('<?xml'):
        try:
            return ctype, 'xml', xml_to_obj(ET.fromstring(body.encode()))
        except ET.ParseError:
            return ctype, 'invalid-xml', body
    return ctype, 'text', body


def status_findings(a, b, findings):
    """Record transport, 5xx and status differences; True if decided."""
    for label, side in (('A', a), ('B', b)):
        if 'error' in side:
            findings.add('error', 'transport error on %s' % label)
        elif side['status'] >= 500:
            findings.add('error', '%d on %s' % (side['status'], label))
    if findings.items:
        return True
    if a['status'] == b['status']:
        return False
    if {a['status'], b['status']} == {200, 404}:
        findings.add('data', 'only %s has the object (200 vs 404)'
                     % ('A' if a['status'] == 200 else 'B'))
    else:
        findings.add('behavioral', 'status %d -> %d'
                     % (a['status'], b['status']))
    return True


def body_findings(a, b, site_hosts, findings):
    ca, fa, va = parse_body(a, site_hosts)
    cb, fb, vb = parse_body(b, site_hosts)
    if ca != cb:
        findings.add('behavioral', 'content-type %s -> %s' % (ca, cb))
    if fa != fb:
        findings.add('behavioral', 'body format %s -> %s' % (fa, fb))
    elif fa in ('json', 'xml'):
        compare_values(va, vb, '', findings)
    elif va != vb:
        findings.add('behavioral' if a['status'] < 400 else 'data',
                     '%s body differs' % fa)


def classify(rec, site_hosts):
    findings = Findings()
    a, b = rec['a'], rec['b']
    if status_findings(a, b, findings):
        return findings
    if 300 <= a['status'] < 400:
        la = scrub(a.get('location') or '', site_hosts)
        lb = scrub(b.get('location') or '', site_hosts)
        if la != lb:
            findings.add('behavioral', 'redirect location differs')
        return findings
    body_findings(a, b, site_hosts, findings)
    return findings


def safe_path(path):
    """Resolve a path given on the command line; it must be under the cwd."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(os.getcwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        sys.exit('%s is outside the current directory' % path)
    return resolved


def load(paths):
    for path in paths:
        with gzip.open(path, 'rt') as fh:
            try:
                for number, line in enumerate(fh, 1):
                    try:
                        yield json.loads(line)
                    except ValueError:
                        print('warning: %s line %d is not valid JSON; '
                              'skipped' % (path, number), file=sys.stderr)
            except (EOFError, OSError):
                print('warning: %s ends in an unfinished record; pairs after '
                      'it are missing' % path, file=sys.stderr)


def sample_urls(directory):
    """(endpoint, url) pairs in a sample directory, to restrict a report."""
    wanted = set()
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.tsv'):
            continue
        with open(os.path.join(directory, name)) as fh:
            for line in fh:
                if line.startswith('#') or '\t' not in line:
                    continue
                wanted.add((name[:-4], line.rstrip('\n').split('\t', 1)[1]))
    return wanted


def build_report(records, site_hosts, examples):
    endpoints = collections.OrderedDict()
    for rec in records:
        findings = classify(rec, site_hosts)
        ep = endpoints.setdefault(rec['endpoint'], {
            'pairs': 0, 'hits': 0,
            'kinds': collections.Counter(),
            'hit_weighted_kinds': collections.Counter(),
            'categories': {},
        })
        kind = findings.kind()
        ep['pairs'] += 1
        ep['hits'] += rec.get('hits', 1)
        ep['kinds'][kind] += 1
        ep['hit_weighted_kinds'][kind] += rec.get('hits', 1)
        for key in sorted(set(findings.items)):
            cat = ep['categories'].setdefault(
                '\t'.join(key), {'kind': key[0], 'category': key[1],
                                 'path': key[2], 'pairs': 0,
                                 'examples': []})
            cat['pairs'] += 1
            if len(cat['examples']) < examples:
                cat['examples'].append(rec['url'])
    for ep in endpoints.values():
        ep['kinds'] = dict(ep['kinds'])
        ep['hit_weighted_kinds'] = dict(ep['hit_weighted_kinds'])
        ep['categories'] = sorted(
            ep['categories'].values(),
            key=lambda c: (-SEVERITY[c['kind']], -c['pairs'], c['category'],
                           c['path']))
    return endpoints


def pct(n, total):
    return '%.1f%%' % (100.0 * n / total) if total else '-'


def markdown(report, max_rows):
    lines = ['# Golden-response diff', '']
    lines.append('| endpoint | pairs | identical | data | behavioral | error |')
    lines.append('|---|---:|---:|---:|---:|---:|')
    for name, ep in report.items():
        k = ep['kinds']
        lines.append('| %s | %d | %s | %s | %s | %s |' % (
            name, ep['pairs'], *['%d (%s)' % (k.get(x, 0),
                                               pct(k.get(x, 0), ep['pairs']))
                                 for x in ('identical', 'data', 'behavioral',
                                           'error')]))
    lines.append('')
    lines.append('A pair counts under the most severe kind among its '
                 'findings. Categories below count pairs, and one pair can '
                 'appear in several.')
    for name, ep in report.items():
        lines += ['', '## %s' % name, '',
                  '| kind | category | path | pairs | example |',
                  '|---|---|---|---:|---|']
        for cat in ep['categories'][:max_rows]:
            example = cat['examples'][0] if cat['examples'] else ''
            if len(example) > 120:
                example = example[:117] + '...'
            lines.append('| %s | %s | `%s` | %d | `%s` |' % (
                cat['kind'], cat['category'], cat['path'] or '-',
                cat['pairs'], example.replace('|', '%7C')))
        hidden = len(ep['categories']) - max_rows
        if hidden > 0:
            lines.append('')
            lines.append('%d more categories in the JSON report.' % hidden)
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('results', nargs='+', help='replay .jsonl.gz files')
    parser.add_argument('--site-host', action='append',
                        help='hostname replaced by <site> before comparing '
                        '(repeatable; default: the prod and stage hosts)')
    parser.add_argument('--sample', nargs='?', const=SAMPLES,
                        help='only report URLs in a sample directory; with '
                        'no value, the committed sample')
    parser.add_argument('--markdown', help='write the markdown report here')
    parser.add_argument('--json', help='write the full JSON report here')
    parser.add_argument('--examples', type=int, default=3)
    parser.add_argument('--max-rows', type=int, default=40)
    args = parser.parse_args()

    # Longest first so a hostname is never partly replaced by a shorter one.
    hosts = sorted(args.site_host or DEFAULT_SITE_HOSTS, key=len,
                   reverse=True)
    records = load([safe_path(p) for p in args.results])
    if args.sample:
        sample_dir = (SAMPLES if args.sample == SAMPLES
                      else safe_path(args.sample))
        wanted = sample_urls(sample_dir)
        records = (r for r in records if (r['endpoint'], r['url']) in wanted)
    report = build_report(records, hosts, args.examples)
    text = markdown(report, args.max_rows)
    if args.markdown:
        with open(safe_path(args.markdown), 'w') as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    if args.json:
        with open(safe_path(args.json), 'w') as fh:
            json.dump(report, fh, indent=2)
            fh.write('\n')


if __name__ == '__main__':
    main()
