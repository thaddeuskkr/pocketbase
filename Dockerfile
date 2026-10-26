# syntax=docker/dockerfile:1

ARG PB_VERSION=0.40.6

FROM --platform=$BUILDPLATFORM alpine:latest AS download

ARG PB_VERSION
ARG TARGETOS
ARG TARGETARCH

SHELL ["/bin/ash", "-eo", "pipefail", "-c"]

# Use current security-patched packages from the selected Alpine release.
# hadolint ignore=DL3018
RUN apk add --no-cache ca-certificates curl unzip

WORKDIR /tmp/pocketbase
RUN set -eu; \
    case "${TARGETOS}/${TARGETARCH}" in \
      linux/amd64|linux/arm64) ;; \
      *) echo "Unsupported platform: ${TARGETOS}/${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    echo "${PB_VERSION}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; \
    archive="pocketbase_${PB_VERSION}_linux_${TARGETARCH}.zip"; \
    release="https://github.com/pocketbase/pocketbase/releases/download/v${PB_VERSION}"; \
    curl --fail --silent --show-error --location --retry 5 "${release}/${archive}" -o "${archive}"; \
    curl --fail --silent --show-error --location --retry 5 "${release}/checksums.txt" -o checksums.txt; \
    awk -v name="${archive}" '$2 == name { print }' checksums.txt > selected.sha256; \
    test "$(wc -l < selected.sha256)" -eq 1; \
    sha256sum -c selected.sha256; \
    unzip "${archive}" -d /out; \
    chmod 0755 /out/pocketbase

FROM alpine:latest

ARG PB_VERSION

LABEL org.opencontainers.image.title="PocketBase" \
      org.opencontainers.image.description="Unofficial container for the official PocketBase release binary" \
      org.opencontainers.image.version="${PB_VERSION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.documentation="https://pocketbase.io/docs/"

# Use current security-patched packages from the selected Alpine release.
# hadolint ignore=DL3018
RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -g 1000 pocketbase \
    && adduser -D -H -u 1000 -G pocketbase -h /pb pocketbase \
    && mkdir -p /pb/pb_data /pb/pb_migrations /pb/pb_hooks /pb/pb_public \
    && chown -R pocketbase:pocketbase /pb

# Keep the upstream license and other release notices alongside the binary.
COPY --from=download /out/ /pb/

WORKDIR /pb
USER 1000:1000

ENV PB_HEALTHCHECK_URL=http://127.0.0.1:8090/api/health

# Generated migrations need persistence too; hooks and public assets are optional mounts.
VOLUME ["/pb/pb_data", "/pb/pb_migrations"]
EXPOSE 8090/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/bin/sh", "-ec", "wget --quiet --spider \"${PB_HEALTHCHECK_URL}\""]

ENTRYPOINT ["/pb/pocketbase"]
CMD ["serve", "--http=0.0.0.0:8090"]
