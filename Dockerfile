# Causinator 9000 — Multi-stage build
# Stage 1: Build the Rust engine binary (Debian for reliable cross-compilation)
# Stage 2: Slim Alpine runtime with engine + Python sources + MCP server

# ── Stage 1: Rust builder ────────────────────────────────────────────────

FROM rust:1.93-bookworm AS builder

# RocksDB / Drasi build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake clang libclang-dev pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Cache dependency builds: copy manifests first, then do a dummy build
COPY Cargo.toml Cargo.lock ./
COPY crates/c9k-engine/Cargo.toml crates/c9k-engine/Cargo.toml
COPY crates/c9k-cli/Cargo.toml crates/c9k-cli/Cargo.toml
COPY crates/c9k-tests/Cargo.toml crates/c9k-tests/Cargo.toml

# Create stub sources so cargo can resolve the workspace
RUN mkdir -p crates/c9k-engine/src crates/c9k-cli/src crates/c9k-tests/src \
    && echo "fn main() {}" > crates/c9k-engine/src/main.rs \
    && echo "" > crates/c9k-engine/src/lib.rs \
    && echo "fn main() {}" > crates/c9k-cli/src/main.rs \
    && echo "" > crates/c9k-tests/src/lib.rs \
    && cargo build --release --package c9k-engine 2>/dev/null || true

# Copy real source and build
COPY crates/ crates/
RUN cargo build --release --package c9k-engine \
    && strip target/release/c9k-engine

# ── Stage 2: Runtime ─────────────────────────────────────────────────────

FROM alpine:latest AS runtime

RUN apk add --no-cache \
    ca-certificates python3 py3-pip curl git bash libgcc libstdc++ gcompat

# Install GitHub CLI
RUN apk add --no-cache github-cli

# Install Python MCP SDK
RUN pip3 install --no-cache-dir --break-system-packages mcp

WORKDIR /app

# Copy engine binary
COPY --from=builder /build/target/release/c9k-engine /usr/local/bin/c9k-engine

# Copy configs, sources, web UI, MCP server
COPY config/ config/
COPY sources/ sources/
COPY web/ web/
COPY mcp-server/ mcp-server/
COPY copilot-extension/ copilot-extension/
COPY docker-entrypoint.sh /usr/local/bin/entrypoint.sh
COPY action-entrypoint.sh /usr/local/bin/action-entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/action-entrypoint.sh

# Engine listens on 8080 inside the container
ENV C9K_ENGINE_URL=http://127.0.0.1:8080
ENV C9K_DRASI_ENABLED=false

EXPOSE 8080 8090

ENTRYPOINT ["entrypoint.sh"]
CMD ["mcp-server"]
