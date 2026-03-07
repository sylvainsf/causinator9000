// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Smoke Tests — Rust port of scripts/smoke_test.py
//!
//! Quick validation that the engine is running, accepts events, and
//! produces diagnoses. These tests require a running engine instance.
//!
//! Run with: cargo test -p c9k-tests --test smoke -- --ignored
//!
//! Prerequisites:
//!   Engine running with topology loaded.
//!   Set C9K_ENGINE_URL if not using default (http://localhost:8080).

use c9k_tests::{InjectMutation, InjectSignal, TestClient};

#[tokio::test]
#[ignore = "requires running engine"]
async fn smoke_health_check() {
    let client = TestClient::from_env();
    let health = client
        .health()
        .await
        .expect("Engine should be responding");

    assert_eq!(health.status, "ok");
    println!(
        "Engine: v{} — {} nodes, {} edges",
        health.version, health.nodes, health.edges
    );
    println!(
        "Active: {} mutations, {} signals",
        health.active_mutations, health.active_signals
    );
}

#[tokio::test]
#[ignore = "requires running engine"]
async fn smoke_inject_mutation() {
    let client = TestClient::from_env();
    assert!(client.is_healthy().await, "Engine should be responding");

    let resp = client
        .inject_mutation(InjectMutation {
            node_id: "ctr-eastus-00-00-00".into(),
            mutation_type: "ImageUpdate".into(),
            timestamp: None,
        })
        .await
        .expect("Should accept mutation");

    assert_eq!(resp.status, "accepted");
}

#[tokio::test]
#[ignore = "requires running engine"]
async fn smoke_inject_signal() {
    let client = TestClient::from_env();
    assert!(client.is_healthy().await, "Engine should be responding");

    let resp = client
        .inject_signal(InjectSignal {
            node_id: "ctr-eastus-00-00-00".into(),
            signal_type: "CrashLoopBackOff".into(),
            value: Some(1.0),
            severity: Some("critical".into()),
        })
        .await
        .expect("Should accept signal");

    assert_eq!(resp.status, "accepted");
}

#[tokio::test]
#[ignore = "requires running engine"]
async fn smoke_diagnose() {
    let client = TestClient::from_env();
    assert!(client.is_healthy().await, "Engine should be responding");

    // Clear any previous state
    client.clear().await.expect("Should clear events");

    // Inject mutation + signal
    client
        .inject_mutation(InjectMutation {
            node_id: "ctr-eastus-00-00-00".into(),
            mutation_type: "ImageUpdate".into(),
            timestamp: None,
        })
        .await
        .expect("Should accept mutation");

    client
        .inject_signal(InjectSignal {
            node_id: "ctr-eastus-00-00-00".into(),
            signal_type: "CrashLoopBackOff".into(),
            value: Some(1.0),
            severity: Some("critical".into()),
        })
        .await
        .expect("Should accept signal");

    // Diagnose
    let diag = client
        .diagnose("ctr-eastus-00-00-00")
        .await
        .expect("Should return diagnosis");

    println!(
        "Diagnosis: {:.1}% confidence, root_cause={:?}",
        diag.confidence * 100.0,
        diag.root_cause
    );

    assert!(
        diag.confidence > 0.5,
        "Should produce high-confidence diagnosis after mutation + signal"
    );

    // Clean up
    client.clear().await.expect("Should clear events");
}
