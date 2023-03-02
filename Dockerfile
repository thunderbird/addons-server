FROM debian:11-slim

ENV PYTHONDONTWRITEBYTECODE=1

# Allow scripts to detect we're running in our own container
RUN touch /addons-server-docker-container

# Add nodesource repository and requirements
ADD docker/nodesource.gpg.key /etc/pki/gpg/GPG-KEY-nodesource
RUN apt-get update && apt-get install -y \
        apt-transport-https              \
        gnupg2                           \
    && rm -rf /var/lib/apt/lists/*
RUN cat /etc/pki/gpg/GPG-KEY-nodesource | apt-key add -
ADD docker/debian-bullseye-nodesource-repo /etc/apt/sources.list.d/nodesource.list
ADD docker/debian-bullseye-backports-repo /etc/apt/sources.list.d/backports.list

RUN apt-get update && apt-get install -y \
        # General (dev-) dependencies
        bash-completion \
        build-essential \
        cmake \
        curl \
        libjpeg-dev \
        libsasl2-dev \
        libxml2-dev \
        libxslt-dev \
        locales \
        zlib1g-dev \
        libffi-dev \
        libssl-dev \
        nodejs \
        # Git, because we're using git-checkout dependencies
        git \
        # Dependencies for mysql-python
        mariadb-server \
        mariadb-client \
        libmariadb-dev \
        libmariadb-dev-compat \
        swig \
        gettext \
        # Use rsvg-convert to render our static theme previews
        librsvg2-bin \
        # Use pngcrush to optimize the PNGs uploaded by developers
        pngcrush \
        # our makefile and ui-tests require uuid to be installed
        uuid \
        # Dependencies \
        memcached \
        nginx \
        #elasticsearch \
        redis-server \
        rabbitmq-server \
        npm \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Compile required locale
RUN localedef -i en_US -f UTF-8 en_US.UTF-8

# Build libgit2-0.27
RUN mkdir -p /build/libgit2
RUN wget -P /build/libgit2 https://github.com/libgit2/libgit2/archive/refs/tags/v0.27.4.tar.gz
RUN cd /build/libgit2 \
    && tar -xf v0.27.4.tar.gz  \
    && cd libgit2-0.27.4  \
    && mkdir build \
    && cd build \
    && cmake .. \
    && cmake --build . --target install

# Build Python 2....
RUN mkdir -p /build/python2
RUN wget -P /build/python2 https://www.python.org/ftp/python/2.7.18/Python-2.7.18.tgz
RUN cd /build/python2  \
    && tar -xf Python-2.7.18.tgz  \
    && cd Python-2.7.18  \
    && ./configure --enable-optimizations \
    && make -j ${nproc} . \
    && make install

ENV PYTHON_PIP_VERSION=20.0.2
ENV PYTHON_GET_PIP_URL=https://github.com/pypa/get-pip/raw/d59197a3c169cef378a22428a3fa99d33e080a5d/get-pip.py
ENV PYTHON_GET_PIP_SHA256=421ac1d44c0cf9730a088e337867d974b91bdce4ea2636099275071878cc189e
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Test it out!
RUN python --version
RUN python -m ensurepip --upgrade
RUN python -m pip install --upgrade pip

# Set the locale. This is mainly so that tests can write non-ascii files to
# disk.
ENV LANG en_US.UTF-8
ENV LC_ALL en_US.UTF-8

COPY . /code
WORKDIR /code

ENV PIP_BUILD=/deps/build/
ENV PIP_CACHE_DIR=/deps/cache/
ENV PIP_SRC=/deps/src/
ENV NPM_CONFIG_PREFIX=/deps/
ENV SWIG_FEATURES="-D__x86_64__"

# Install all python requires
RUN mkdir -p /deps/{build,cache,src}/ && \
    ln -s /code/package.json /deps/package.json && \
    make update_deps && \
    rm -rf /deps/build/ /deps/cache/

# Preserve bash history across image updates.
# This works best when you link your local source code
# as a volume.
ENV HISTFILE /code/docker/artifacts/bash_history

# Configure bash history.
ENV HISTSIZE 50000
ENV HISTIGNORE ls:exit:"cd .."

# This prevents dupes but only in memory for the current session.
ENV HISTCONTROL erasedups

ENV CLEANCSS_BIN /deps/node_modules/.bin/cleancss
ENV LESS_BIN /deps/node_modules/.bin/lessc
ENV UGLIFY_BIN /deps/node_modules/.bin/uglifyjs
ENV ADDONS_LINTER_BIN /deps/node_modules/.bin/addons-linter
RUN npm cache clean -f && npm install -g n && /deps/bin/n 14.21

# Add our nginx config
ADD docker/etc/nginx/sites-available/atn.conf /etc/nginx/sites-available/atn.conf

# Add our mariadb config
ADD docker/etc/mysql/my.cnf /etc/mysql/my.cnf
ADD docker/etc/mysql/mariadb.conf.d/99-remote.cnf /etc/mysql/mariadb.conf.d/99-remote.cnf

# Add our rabbitmq config
ADD docker/etc/rabbitmq/rabbitmq.conf /etc/rabbitmq/rabbitmq.conf

# Add our redis config
ADD docker/etc/redis/redis.conf /etc/redis/redis.conf
