// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Checkpoint Manager — Bincode serialization of solver state.
//!
//! Writes a checkpoint after each mutation batch is fully processed.
//! On startup, the engine can optionally load a prior checkpoint.

use anyhow::{Context, Result};
use std::path::Path;

/// Write solver state to disk as bincode.
pub fn write_checkpoint<T: serde::Serialize>(state: &T, path: &str) -> Result<()> {
    let bytes = bincode::serde::encode_to_vec(state, bincode::config::standard())
        .context("serializing checkpoint")?;
    std::fs::write(path, &bytes).context(format!("writing checkpoint to {path}"))?;
    tracing::info!(path, bytes = bytes.len(), "Checkpoint written");
    Ok(())
}

/// Read solver state from a bincode checkpoint file.
pub fn read_checkpoint<T: serde::de::DeserializeOwned>(path: &str) -> Result<T> {
    let bytes = std::fs::read(path).context(format!("reading checkpoint from {path}"))?;
    let (state, _): (T, _) =
        bincode::serde::decode_from_slice(&bytes, bincode::config::standard())
            .context("deserializing checkpoint")?;
    tracing::info!(path, bytes = bytes.len(), "Checkpoint loaded");
    Ok(state)
}

/// Check if a checkpoint file exists.
pub fn checkpoint_exists(path: &str) -> bool {
    Path::new(path).exists()
}
