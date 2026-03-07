// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Causinator 9000 Engine — Reactive Causal Inference Engine for cloud infrastructure.
//!
//! This library provides the core Bayesian solver, REST API, checkpoint persistence,
//! and Drasi CDC integration for causal root-cause analysis.

pub mod api;
pub mod checkpoint;
pub mod drasi;
pub mod solver;

/// Default PostgreSQL port for Causinator 9000 (avoids conflict with other local PG instances)
pub const PG_PORT: u16 = 5433;
