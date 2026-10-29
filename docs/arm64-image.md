# arm64 image for stage-eks

Tracking issue: [thunderbird/addons-server#570](https://github.com/thunderbird/addons-server/issues/570).

The `stage-eks` image is `Dockerfile.python3.deploy` built for `linux/arm64`
(mzla-eks-tb-dev01 runs Graviton nodes). The same Dockerfile still builds for
`linux/amd64`; every arm64-specific step is gated on `TARGETARCH` and is a
no-op on x86.

## Build

Local, on Apple silicon (native, no emulation):

    docker buildx build --platform linux/arm64 -f Dockerfile.python3.deploy -t addons-server:arm64 --load .

CI: `.github/workflows/build-and-push-stage-eks.yml` builds on the
GitHub-hosted `ubuntu-24.04-arm` runner. Pull requests to `stage-eks` build and
smoke-test only. Pushes to `stage-eks` push to
`718959508124.dkr.ecr.eu-central-1.amazonaws.com/atn/addons-server` tagged
`<commit sha>` and `stage-eks`, authenticating with GitHub OIDC through the
role in the `AWS_ROLE_ARN_TB_DEV` repository variable (optional
`ECR_REPOSITORY_TB_DEV` overrides the repository name).

## Substitutions

| Component | x86_64 | arm64 | Change |
|---|---|---|---|
| Base image `python:3.6-slim-stretch` | amd64 manifest | `arm64/v8` manifest of the same tag | none |
| Debian stretch apt packages (archive.debian.org) | amd64 | arm64 builds of the same versions, including the `libssl1.1=1.1.0l-1~deb9u1` downgrade | none |
| nodesource `node_10.x stretch` | amd64 | arm64 package from the same repository | none |
| `SWIG_FEATURES=-D__x86_64__` | - | - | none; inert, no Python 3 requirement uses swig (M2Crypto was Python 2 only) |
| mysqlclient 1.3.14, Pillow 6.2.2, PyYAML 3.13, maxminddb, cffi, cryptography 2.5, uWSGI 2.0.18 | wheel or sdist | sdist compiled in the image; hashes already pinned | none |
| lxml 4.3.2 | `cp36m-manylinux1_x86_64` wheel | no aarch64 wheel; compiled from the sdist against stretch `libxml2-dev`/`libxslt-dev` | added the sdist sha256 (`3a9d8521...c05f`, from PyPI) to `requirements/prod_common.txt`; the x86 wheel hash is unchanged |
| pygit2 0.28.2 | manylinux wheel with libgit2 bundled | no aarch64 wheel; the sdist needs libgit2 0.28, stretch has 0.24 and stretch-backports 0.27.4 | `Dockerfile.python3.deploy` builds libgit2 0.28.2 from the upstream tag tarball (sha256 `42b5f1e9...7fc3`, matches spack and homebrew-core) when `TARGETARCH=arm64`; cmake is installed for the build and purged after |
| uglify-js | 3.8.1 | 3.8.1 | bumped to 3.17.4 on both architectures, see below |

### uglify-js 3.8.1 to 3.17.4 (both architectures)

Not an arm64 issue, found by the compress_assets smoke. uglify-js 3.8.1 only
parses ES5, and `compress_assets` ignores its exit code. ES2015 syntax left in
`static/js/impala/{abuse,ajaxcache,footer,homepage,collections}.js` after
[thunderbird/addons-server#455](https://github.com/thunderbird/addons-server/pull/455)
made uglify fail on the `common` and `impala` bundles, so `common-min.js` and
`impala-min.js` were never written. With `DEBUG = False` the templates load
those `-min.js` files, so on any deployed stage the site JavaScript would 404.
(The `debug` bundle, `node_lib/less.js`, also failed; it only loads when
`DEBUG = True`.)

3.17.4 parses ES2015+ and does not default to `module` mode. The current
3.19.3 parses input as a strict-mode ES module by default (`--no-module` turns
that off) and rejects the `with` statement in the precompiled underscore templates
(`zamboni/files_templates.js`, `zamboni/themes_review_templates.js`). With
3.17.4 all 124 JS files in `MINIFY_BUNDLES` parse, checked one file at a time.

The bump needs one code change: `compress_assets._minify` called
`uglifyjs -v -o <target> <source> -m`. 3.8.1 ignored the undocumented `-v`;
3.17.4 reads `-v` as `--version`, prints the version, exits 0 and writes
nothing, which `compress_assets` would also have hidden. The `-v` is dropped.

The x86 build of the unchanged `stage-eks` tree shows the same three uglify
failures (`common-all.js`, `impala-all.js`, `debug-all.js`), so this is an
existing `stage-eks` bug and not arm64-specific.

## Smoke results

Commit `69e0982750` built natively for arm64 on Apple silicon, and for amd64
under emulation from the same tree. Both images give the same results:

- `python manage.py check`: no issues (20 silenced).
- `python manage.py compress_assets --force`: 11 JS bundles (UglifyJS) and
  13 CSS bundles (clean-css) minified, no parse errors. Every `*-all.js` has
  a `*-min.js` next to it.
- `import olympia.lib.es`: ok. pygit2 0.28.2 against libgit2 0.28.2.
- PyYAML is not in the image: it is only pinned in `requirements/tests.txt`,
  not in the prod requirements, on either architecture.

`pip freeze`, amd64 vs arm64: 105 packages each, identical (empty diff).
The only architecture difference below Python is where libgit2 comes from
(bundled in the pygit2 wheel on x86, built in the image on arm64); both
report 0.28.2.

To reproduce, with `settings_local.py` from `Dockerfile.python3.deploy`
on `PYTHONPATH`:

    python manage.py check
    python manage.py compress_assets --force 2>&1 | grep -iE 'parse error|^ERROR'
    python -c "import olympia.lib.es"
    pip freeze | sort
