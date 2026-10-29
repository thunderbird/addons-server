#!/bin/sh
# Smoke-test an addons-server image built from Dockerfile.python3.deploy
# (thunderbird/addons-server#570). Usage: smoke-stage-eks-image.sh <image>
set -eu
image="$1"

docker run --rm "$image" uname -m
docker run --rm -e DJANGO_SETTINGS_MODULE=settings "$image" \
    python manage.py check
docker run --rm -e DJANGO_SETTINGS_MODULE=settings "$image" \
    python -c "import olympia.lib.es, MySQLdb, lxml.etree, PIL.Image, pygit2; print('imports ok')"
# Every JS bundle must have minified output.
docker run --rm "$image" sh -c \
    'rc=0; for f in $(find static -name "*-all.js"); do [ -s "${f%-all.js}-min.js" ] || { echo "missing ${f%-all.js}-min.js"; rc=1; }; done; exit $rc'
echo "smoke ok: $image"
