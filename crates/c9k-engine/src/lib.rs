// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Causinator 9000 Engine — Reactive Causal Inference Engine for cloud infrastructure.
//!
//! This library provides the core Bayesian solver, REST API, checkpoint persistence,
//! and Drasi CDC integration for causal root-cause analysis.

pub mod api;
pub mod checkpoint;
pub mod drasi;
pub mod ingest;
pub mod mcp;
pub mod solver;

/// Embedded heuristic YAML files — compiled into the binary.
pub mod embedded_heuristics {
    pub const CONTAINERS: &str = include_str!("../../../config/heuristics/containers.yaml");
    pub const COMPUTE: &str = include_str!("../../../config/heuristics/compute.yaml");
    pub const NETWORKING: &str = include_str!("../../../config/heuristics/networking.yaml");
    pub const ROUTING: &str = include_str!("../../../config/heuristics/routing.yaml");
    pub const DATABASES: &str = include_str!("../../../config/heuristics/databases.yaml");
    pub const IDENTITY: &str = include_str!("../../../config/heuristics/identity.yaml");
    pub const MESSAGING: &str = include_str!("../../../config/heuristics/messaging.yaml");
    pub const PHYSICAL_INFRA: &str = include_str!("../../../config/heuristics/physical-infra.yaml");
    pub const APPLICATIONS: &str = include_str!("../../../config/heuristics/applications.yaml");
    pub const CI_PIPELINES: &str = include_str!("../../../config/heuristics/ci-pipelines.yaml");
    pub const KUBERNETES: &str = include_str!("../../../config/heuristics/kubernetes.yaml");

    /// All standard heuristic layers in load order.
    pub const ALL: &[&str] = &[
        CONTAINERS, COMPUTE, NETWORKING, ROUTING, DATABASES,
        IDENTITY, MESSAGING, PHYSICAL_INFRA, APPLICATIONS,
        CI_PIPELINES, KUBERNETES,
    ];
}

/// Default PostgreSQL port for Causinator 9000 (avoids conflict with other local PG instances)
pub const PG_PORT: u16 = 5433;
