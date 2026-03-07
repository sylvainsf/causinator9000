-- RCIE POC: PostgreSQL Schema
-- Run: psql rcie_poc < scripts/schema.sql

-- Topology (written by LLM transpiler, static for POC)
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    class       TEXT NOT NULL,
    region      TEXT,
    rack_id     TEXT,
    properties  JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES nodes(id),
    target_id   TEXT NOT NULL REFERENCES nodes(id),
    edge_type   TEXT NOT NULL,
    properties  JSONB DEFAULT '{}'
);

-- Mutations (written by Radius webhook receiver)
CREATE TABLE IF NOT EXISTS mutations (
    id              TEXT PRIMARY KEY,
    node_id         TEXT NOT NULL,
    mutation_type   TEXT NOT NULL,
    source          TEXT DEFAULT 'radius',
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    properties      JSONB DEFAULT '{}'
);

-- Signals (written by Azure Monitor webhook receiver)
CREATE TABLE IF NOT EXISTS signals (
    id              TEXT PRIMARY KEY,
    node_id         TEXT NOT NULL,
    signal_type     TEXT NOT NULL,
    value           DOUBLE PRECISION,
    severity        TEXT,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    properties      JSONB DEFAULT '{}'
);

-- Indexes for Drasi CQ performance
CREATE INDEX IF NOT EXISTS idx_mutations_node_id ON mutations(node_id);
CREATE INDEX IF NOT EXISTS idx_mutations_timestamp ON mutations(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_node_id ON signals(node_id);
CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
