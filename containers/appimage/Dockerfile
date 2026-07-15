FROM debian:11-slim@sha256:cba95a21c96c1f5fc2470081829363eed57706634f7dc26e8c6712934303d57a

ARG PIXI_VERSION=0.72.0
ARG PIXI_SHA256_AMD64=2c086608809f7bdd9918323cf6f6278bb43b025f4d957ddfd55295cf151c6f21
ARG PIXI_SHA256_ARM64=8b48fd8b315552ee48d340e89d654a177d1f001810ab741f51f7dcdd7e00e1c1

RUN set -eux; \
    apt-get update; \
    apt-get install --yes --no-install-recommends \
        binutils \
        ca-certificates \
        curl \
        file \
        git \
        libatomic1 \
        libdbus-1-3 \
        libegl1 \
        libfontconfig1 \
        libfreetype6 \
        libgl1 \
        libglib2.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb-cursor0 \
        libxcb-icccm4 \
        libxcb-keysyms1 \
        libxcb-randr0 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-xinerama0 \
        libxcb-xkb1 \
        libxkbcommon-x11-0 \
        tar; \
    rm -rf /var/lib/apt/lists/*; \
    architecture="$(dpkg --print-architecture)"; \
    case "$architecture" in \
        amd64) pixi_architecture="x86_64"; pixi_sha256="$PIXI_SHA256_AMD64" ;; \
        arm64) pixi_architecture="aarch64"; pixi_sha256="$PIXI_SHA256_ARM64" ;; \
        *) echo "Unsupported container architecture: $architecture" >&2; exit 1 ;; \
    esac; \
    pixi_archive="pixi-${pixi_architecture}-unknown-linux-musl.tar.gz"; \
    curl --fail --location --retry 3 \
        "https://github.com/prefix-dev/pixi/releases/download/v${PIXI_VERSION}/${pixi_archive}" \
        --output "/tmp/${pixi_archive}"; \
    echo "${pixi_sha256}  /tmp/${pixi_archive}" | sha256sum --check -; \
    tar --extract --gzip --file "/tmp/${pixi_archive}" --directory /usr/local/bin pixi; \
    rm "/tmp/${pixi_archive}"; \
    pixi --version

COPY build.sh /usr/local/bin/pandrator-build-appimage

RUN chmod 0755 /usr/local/bin/pandrator-build-appimage; \
    mkdir -p /work /tmp/pandrator-builder /tmp/pixi-cache /tmp/pixi-home; \
    chmod 1777 /work /tmp/pandrator-builder /tmp/pixi-cache /tmp/pixi-home

ENV HOME=/tmp/pandrator-builder \
    PIXI_CACHE_DIR=/tmp/pixi-cache \
    PIXI_HOME=/tmp/pixi-home

ENTRYPOINT ["/usr/local/bin/pandrator-build-appimage"]
