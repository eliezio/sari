FROM python:3.8.4-slim-buster as stage0

RUN set -uex; \
    apt-get update -yqq; \
    apt-get upgrade -yq; \
    rm -rfv \
        /var/lib/apt/lists/* \
        /var/cache/debconf/* \
        /var/log/dpkg.log \
        /var/log/lastlog

FROM scratch
LABEL maintainer="Eliezio Oliveira <eliezio@pm.me>"
COPY --from=stage0 / /

ENV HOME=/app
RUN set -eux; \
    adduser --disabled-password --home $HOME --gecos Pulumi pulumi; \
    rm -vf /var/log/lastlog
WORKDIR $HOME
ENV PATH=$HOME/.pulumi/bin:/usr/local/bin:/usr/bin:/bin

# Install Pulumi & Plugins in one go
# Optmizations that saves 175MB:
#   1. Removed non-used language-oriented runtimes
#   2. Stripped binaries
# Modified folders: $HOME/.pulumi /usr/local/lib/python3.8
COPY --chown=pulumi:pulumi Pipfile* ./
RUN set -eux; \
    # 0 [System]
    # 0.0 Packages update
    apt-get update -yqq; \
    # 0.1 Install installers
    apt-get install -yq --no-install-recommends binutils curl jq openssh-client; \
    # 1 [Pulumi]
    # 1.1 Install Pulumi & Plugins
    pulumi_version=$(jq -r .default.pulumi.version Pipfile.lock | sed -e 's/^==//'); \
    curl --proto '=https' --tlsv1.2 -fsSL https://get.pulumi.com/ | sh -s -- --version $pulumi_version; \
    chown -R pulumi $HOME/.pulumi; \
    for p in aws mysql random; do \
        version=$(jq -r .default.\"pulumi-$p\".version Pipfile.lock | sed -e 's/^==//'); \
        su pulumi -c "$HOME/.pulumi/bin/pulumi plugin install resource $p $version"; \
    done; \
    # 1.2 Remove unused
    for lang in dotnet go nodejs; do \
        rm -vf $HOME/.pulumi/bin/pulumi-language-$lang \
               $HOME/.pulumi/bin/pulumi-resource-pulumi-$lang; \
    done; \
    # 1.3 Strip executables
    strip --strip-unneeded --preserve-dates \
        $HOME/.pulumi/bin/pulumi \
        $HOME/.pulumi/bin/pulumi-language-python; \
    find $HOME/.pulumi/plugins -type f -executable \
        -exec strip --strip-unneeded --preserve-dates {} \; ; \
    # 1.4 Fix ownership and permissions
    chown -R pulumi:pulumi $HOME; \
    chmod -R go=u-w $HOME; \
    # 2 [Python]
    # 2.0 Install installers
    pip3 install --disable-pip-version-check --no-cache-dir pipenv; \
    # 2.1 Install packages
    su pulumi -c "pipenv install --system"; \
    # 2.2 Uninstall installers
    pip3 uninstall --disable-pip-version-check --yes pipenv virtualenv virtualenv-clone; \
    # 2.3 Remove unused
    rm -rv $HOME/.local/lib/python3.8/site-packages/mysql-vendor; \
    find $HOME/.local -type d -name __pycache__ \
        -exec rm -rf {} \; -prune; \
    find /usr/local/lib/python3.8 -type d -name __pycache__ \
        -exec rm -rf {} \; -prune; \
    # 2.4 Strip executables
    find $HOME/.local/lib/python3.8 -name \*.so \
        -exec strip --strip-unneeded --preserve-dates {} \; ; \
    # 0.2 Uninstall installers
    apt-get autoremove -yq binutils curl jq; \
    # 0.3 Remove garbage
    rm -rfv \
        $HOME/.cache \
        /var/lib/apt/lists/* \
        /var/cache/debconf/* \
        /var/log/dpkg.log

# The default (1024) if not enough for this Pulumi project
RUN echo "* soft nofile 4096" | tee -a /etc/security/limits.conf

USER pulumi

# Copy application
COPY --chown=pulumi:pulumi entrypoint.sh run-proxy.sh *.py Pulumi.yaml ./
COPY --chown=pulumi:pulumi main/ $HOME/main/

ENTRYPOINT [ "./entrypoint.sh" ]
CMD [ "preview" ]
