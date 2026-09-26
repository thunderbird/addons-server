# Golden-response harness for the Thunderbird client endpoints

Replays real Thunderbird client requests against two deployments and reports
how their responses differ, split into **data drift** (same response shape,
different content) and **behavioral drift** (different shape, status,
content type or protocol). Built for
[thunderbird/addons-server#398](https://github.com/thunderbird/addons-server/issues/398)
and reused at each cutover step.

Python 3.6+ standard library only (tested on 3.6 and 3.9). `sample.py` also
needs the `aws` CLI.

| file | purpose |
|---|---|
| `sample.py` | builds the request sample from CloudFront standard logs |
| `samples/*.tsv` | the committed sample: `hits<TAB>url`, one distinct URL per line |
| `samples/manifest.json` | the log window, every log file read, request counts |
| `replay.py` | fetches every sample URL from target A and target B, records the responses |
| `compare.py` | normalizes and classifies the recorded pairs, writes the report |

## Endpoints

| endpoint | path | served by (prod) | sample |
|---|---|---|---|
| `versioncheck` | `/update/VersionCheck.php` | `versioncheck.addons.thunderbird.net` (CloudFront `E1PQMC7BGJOP5E`) | 10,000 distinct query strings |
| `api-v4` | `/api/v4/` GET | `services.addons.thunderbird.net` (CloudFront `EGX44RIFURRUW`) | 2,000 distinct paths |
| `api-v3` | `/api/v3/` GET | `services.addons.thunderbird.net` | 2,000 distinct paths |

`/api/v4/` is a small share of services traffic (about 34k of 10M requests in
the committed sample's files). Most of it is `addons/addon/<id>/`, then
`discovery/` and `addons/search/`. `/api/v3/` carries the add-on manager's
`addons/search/?guid=...` lookups, which is where most client traffic goes,
so it is sampled as well.

## Running it

File paths given on the command line (`--out`, `--samples`, `--markdown`,
`--json`, and the results files) must be under the current directory; the
scripts refuse anything else. Run from a scratch directory and call the
scripts by path. The committed sample is used by default wherever you run
from:

```sh
REPO=~/work/thunderbird/addons-server   # your checkout
mkdir -p /tmp/golden && cd /tmp/golden
```

### 1. Sample (optional; the committed sample is the reference)

```sh
python3 $REPO/scripts/golden/sample.py --profile mzla-tb-legacy --end 2026-09-25 --days 7 --files-per-hour 3
```

Without `--out` this rewrites `$REPO/scripts/golden/samples/`.

This reads the logs with read-only S3 calls in the thunderbird-legacy account:
`s3://versioncheck-logs/E1PQMC7BGJOP5E.*` and
`s3://services-addons-logs/services-logs/EGX44RIFURRUW.*`. The logs rotate
about every 5 minutes and expire after 90 days. For each hour in the window
it reads the `--files-per-hour` files with the smallest sha256(key), then
keeps the distinct URLs with the smallest sha256(url) (bottom-k), so a run
over the same logs always produces the same sample. Commit the resulting
`samples/` directory. Once the logs have expired, the committed files are
the only copy of the sample.

What is kept: the URL path, the query string exactly as the client sent it
(CloudFront's extra layer of logging percent-encoding is undone), and the
hit count within the files read. What is never kept: client IP, user agent,
cookies, referer, edge request id and every other log field. Requests
carrying a credential-like parameter (`token`, `email`, `sig`, ...) or
naming an add-on id that looks like a personal webmail address are dropped
whole. Nothing is edited out of a kept URL, because that would change the
response.

The address filter costs some coverage. Plenty of listed add-ons use the
developer's personal address as their id (for example
`marcoagpinto@mail.telepac.pt`, a popular dictionary), and every request
naming one is dropped: about 3% of versioncheck and `/api/v3/` requests in
the committed sample. `manifest.json` records the count as
`dropped_sensitive`. The filter is there because self-distributed add-on
ids show up in versioncheck traffic too, and this repo is public.

### 2. Replay

```sh
python3 $REPO/scripts/golden/replay.py --out prod-vs-stage.jsonl.gz
```

The defaults are target A = production (`versioncheck.addons.thunderbird.net`,
`services.addons.thunderbird.net`) and target B = the Fargate stage
(`versioncheck.addons-stage.thunderbird.net`, `addons-stage.thunderbird.net`).
Point either side somewhere else with `--a-versioncheck`, `--a-services`,
`--b-versioncheck` and `--b-services`, for example EKS stage at cutover.

TLS is always verified. The Fargate stage versioncheck ALB serves the
`*.addons.thunderbird.net` certificate, which doesn't cover
`versioncheck.addons-stage.thunderbird.net`. For that host, the replayer
sends SNI for `versioncheck.addons.thunderbird.net` and checks the name
against that instead, still validating the full chain. Add other
exceptions with `--tls-name HOST=NAME`.

Production safety is built in and can't be loosened from the command line:

- Any host that doesn't look like stage, dev or local is treated as
  production. It is capped at **2 requests per second**, and the run
  **stops at the first 5xx** from it, or after 5 consecutive transport
  errors.
- Only GET requests are sent, redirects are not followed, and no cookies
  are sent.
- Non-production hosts run at `--rps` (default 5).

At 2 rps the full sample (14,000 URLs) takes about 2 hours.
Use `--limit N` for a quick run. `--resume` continues an interrupted run in
the same output file, skipping URLs already recorded. Every record carries
the base URLs it was fetched from, so resuming against different targets is
refused rather than mixing deployments. A record cut off by an interruption
is dropped before new records are appended. After the sample is
regenerated, `--resume` fetches only the URLs that are new to it.

### 3. Compare

```sh
python3 $REPO/scripts/golden/compare.py prod-vs-stage.jsonl.gz \
    --markdown report.md --json report.json
```

`compare.py` makes no network calls, so you can tune it and rerun it on
recorded results as many times as you like. `--sample` on its own limits the
report to the URLs in the committed sample, and `--sample DIR` to another
sample directory under the current one. Use it when a results file also
holds URLs from an older sample.

## How differences are classified

Both sides are normalized first:

- Site hostnames (`--site-host`, default all prod and stage hostnames)
  become `<site>`.
- ISO-8601 and HTTP-date timestamps become `<timestamp>`.
- 32-hex request ids become `<request-id>`.
- JSON key order and XML attribute order are ignored.
- Response headers other than `Content-Type` and `Location` are ignored.
  `Date`, `Expires`, `X-AMO-Request-Id`, CloudFront headers and `ETag` all
  differ legitimately.

JSON bodies are compared by walking both trees. XML bodies, meaning
`reqVersion=1` RDF update manifests, are converted to trees and walked the
same way. Paths are generalized: list indices become `[]`, and keys of
data-keyed objects (locales, add-on guids, numeric ids, app names) become
`{}`. The report therefore aggregates on paths like
`addons{}.updates[].update_hash`.

| kind | meaning | examples |
|---|---|---|
| `identical` | equal after normalization | |
| `data` | same shape, different content: what you expect from a DB copied from prod in 2024 | `value differs`, `list length differs`, `list empty in B`, `null in B`, `map key only in A`, `optional field only in A`, `only A has the object (200 vs 404)` |
| `behavioral` | different shape or protocol, which needs an explanation | `field only in A/B`, `type string -> number`, `status 200 -> 302`, `content-type ...`, `body format json -> text`, `redirect location differs` |
| `error` | 5xx or transport error on either side | `502 on B`, `transport error on A` |

A pair counts under its most severe finding. Categories count pairs, and a
pair usually has several. The JSON report has every category with example
URLs. `hit_weighted_kinds` weights each pair by its hit count in the sample,
which gives a rough idea of how much real traffic each kind covers.

Fields that the server itself includes or leaves out depending on the row
are listed in `OPTIONAL_PATHS` in `compare.py`. For versioncheck (see
`services/update.py`) these are `update_info_url` (only when the version has
release notes), `update_hash`, `strict_max_version` (only for strict
compatibility), and the whole `addons` object, which is `{}` when the add-on
is unknown. The `reqVersion=1` RDF equivalents, `updateHash` and
`updateInfoURL`, are in `OPTIONAL_XML_TAGS`. When one of these is present on
one side only, it counts as data. Any other field present on one side only counts as behavioral.

Known limits of the heuristics:

- Lists are compared position by position, so reordered results show up as
  `value differs`. That is still data, but it is noisy.
- A field that only some items in a list carry, such as a field that only
  themes have, can show up as `field only in A` when the two sides return
  different items. Before treating such a finding as a code change, check
  that it holds across many pairs.
- An environment gap such as an empty search index on stage shows up as
  data (`list empty in B`) even though no code changed. Read the categories,
  not just the totals.
