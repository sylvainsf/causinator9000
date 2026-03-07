// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Variable Elimination algorithm for exact Bayesian inference.
//!
//! Operates on the active subgraph of the causal DAG. Complexity is
//! O(N · e^w) where w is the treewidth of the subgraph.

use std::collections::HashMap;

/// A factor in the factor graph (represents a CPT or observed evidence).
#[derive(Debug, Clone)]
pub struct Factor {
    /// Variable IDs this factor covers
    pub variables: Vec<String>,
    /// Flat probability table indexed by variable assignments
    /// For a 2-variable binary factor: [P(0,0), P(0,1), P(1,0), P(1,1)]
    pub table: Vec<f64>,
}

impl Factor {
    /// Create a new factor with given variables and table.
    pub fn new(variables: Vec<String>, table: Vec<f64>) -> Self {
        Self { variables, table }
    }

    /// Multiply two factors together (factor product).
    pub fn product(&self, other: &Factor) -> Factor {
        // Combine variable sets (union, preserving order)
        let mut combined_vars = self.variables.clone();
        for v in &other.variables {
            if !combined_vars.contains(v) {
                combined_vars.push(v.clone());
            }
        }

        let n = combined_vars.len();
        let table_size = 1 << n; // 2^n for binary variables
        let mut result_table = vec![1.0; table_size];

        for (assignment, entry) in result_table.iter_mut().enumerate() {
            // Map this assignment to indices in self and other
            let self_idx = self.project_assignment(assignment, &combined_vars);
            let other_idx = other.project_assignment(assignment, &combined_vars);

            *entry = self.table[self_idx] * other.table[other_idx];
        }

        Factor {
            variables: combined_vars,
            table: result_table,
        }
    }

    /// Sum out (marginalize) a variable from this factor.
    pub fn marginalize(&self, variable: &str) -> Factor {
        let var_pos = match self.variables.iter().position(|v| v == variable) {
            Some(pos) => pos,
            None => return self.clone(), // Variable not in this factor
        };

        let remaining_vars: Vec<String> = self
            .variables
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != var_pos)
            .map(|(_, v)| v.clone())
            .collect();

        let n_remaining = remaining_vars.len();
        let result_size = 1 << n_remaining;
        let mut result_table = vec![0.0; result_size];

        let n = self.variables.len();
        for assignment in 0..self.table.len() {
            // Compute the assignment index with the marginalized variable removed
            let mut remaining_idx = 0;
            let mut bit = 0;
            for i in 0..n {
                if i == var_pos {
                    continue;
                }
                if assignment & (1 << i) != 0 {
                    remaining_idx |= 1 << bit;
                }
                bit += 1;
            }
            result_table[remaining_idx] += self.table[assignment];
        }

        Factor {
            variables: remaining_vars,
            table: result_table,
        }
    }

    /// Project a global assignment (over combined_vars) to this factor's local index.
    fn project_assignment(&self, global_assignment: usize, combined_vars: &[String]) -> usize {
        let mut local_idx = 0;
        for (local_bit, var) in self.variables.iter().enumerate() {
            let global_bit = combined_vars.iter().position(|v| v == var).unwrap();
            if global_assignment & (1 << global_bit) != 0 {
                local_idx |= 1 << local_bit;
            }
        }
        local_idx
    }
}

/// Run Variable Elimination to compute P(query | evidence).
///
/// # Arguments
/// * `factors` — All CPT factors in the active subgraph
/// * `query` — The variable we want the posterior for
/// * `evidence` — Observed variable assignments (variable → value index)
/// * `elimination_order` — Order in which to eliminate hidden variables
pub fn variable_elimination(
    factors: &[Factor],
    query: &str,
    evidence: &HashMap<String, usize>,
    elimination_order: &[String],
) -> Vec<f64> {
    // 1. Condition on evidence: reduce factors by fixing observed variables
    let mut active_factors: Vec<Factor> = factors
        .iter()
        .map(|f| condition_factor(f, evidence))
        .collect();

    // 2. Eliminate hidden variables one by one
    for var in elimination_order {
        if var == query {
            continue; // Don't eliminate the query variable
        }

        // Collect all factors that mention this variable
        let (relevant, remaining): (Vec<Factor>, Vec<Factor>) = active_factors
            .into_iter()
            .partition(|f| f.variables.contains(var));

        if relevant.is_empty() {
            active_factors = remaining;
            continue;
        }

        // Multiply all relevant factors together
        let mut product = relevant[0].clone();
        for f in &relevant[1..] {
            product = product.product(f);
        }

        // Sum out the variable
        let new_factor = product.marginalize(var);

        active_factors = remaining;
        active_factors.push(new_factor);
    }

    // 3. Multiply remaining factors
    if active_factors.is_empty() {
        return vec![1.0];
    }

    let mut result = active_factors[0].clone();
    for f in &active_factors[1..] {
        result = result.product(f);
    }

    // 4. Normalize
    let sum: f64 = result.table.iter().sum();
    if sum > 0.0 {
        result.table.iter().map(|&p| p / sum).collect()
    } else {
        result.table
    }
}

/// Condition a factor on observed evidence by zeroing out inconsistent entries.
fn condition_factor(factor: &Factor, evidence: &HashMap<String, usize>) -> Factor {
    let mut new_table = factor.table.clone();

    for (var, &observed_val) in evidence {
        if let Some(bit_pos) = factor.variables.iter().position(|v| v == var) {
            for (idx, entry) in new_table.iter_mut().enumerate() {
                let var_val = (idx >> bit_pos) & 1;
                if var_val != observed_val {
                    *entry = 0.0;
                }
            }
        }
    }

    Factor {
        variables: factor.variables.clone(),
        table: new_table,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_simple_factor_product() {
        let f1 = Factor::new(vec!["A".to_string()], vec![0.3, 0.7]);
        let f2 = Factor::new(vec!["A".to_string()], vec![0.6, 0.4]);
        let product = f1.product(&f2);
        assert_eq!(product.variables.len(), 1);
        assert!((product.table[0] - 0.18).abs() < 1e-9);
        assert!((product.table[1] - 0.28).abs() < 1e-9);
    }

    #[test]
    fn test_marginalize() {
        // P(A,B) table for 2 binary vars: [P(A=0,B=0), P(A=1,B=0), P(A=0,B=1), P(A=1,B=1)]
        let f = Factor::new(
            vec!["A".to_string(), "B".to_string()],
            vec![0.1, 0.2, 0.3, 0.4],
        );
        let marginal = f.marginalize("B");
        assert_eq!(marginal.variables, vec!["A".to_string()]);
        // P(A=0) = P(A=0,B=0) + P(A=0,B=1) = 0.1 + 0.3 = 0.4
        // P(A=1) = P(A=1,B=0) + P(A=1,B=1) = 0.2 + 0.4 = 0.6
        assert!((marginal.table[0] - 0.4).abs() < 1e-9);
        assert!((marginal.table[1] - 0.6).abs() < 1e-9);
    }

    #[test]
    fn test_variable_elimination_simple() {
        // Simple chain: A → B
        // P(A): [0.4, 0.6]
        // P(B|A): [P(B=0|A=0), P(B=0|A=1), P(B=1|A=0), P(B=1|A=1)]
        //       = [0.9,        0.2,         0.1,         0.8]
        let prior_a = Factor::new(vec!["A".to_string()], vec![0.4, 0.6]);
        let cpd_b_given_a = Factor::new(
            vec!["A".to_string(), "B".to_string()],
            vec![0.9, 0.2, 0.1, 0.8],
        );

        // Query: P(A | B=1)
        let factors = vec![prior_a, cpd_b_given_a];
        let mut evidence = HashMap::new();
        evidence.insert("B".to_string(), 1);

        let result = variable_elimination(
            &factors,
            "A",
            &evidence,
            &["B".to_string()],
        );

        // P(A=0, B=1) = 0.4 * 0.1 = 0.04
        // P(A=1, B=1) = 0.6 * 0.8 = 0.48
        // Normalized: P(A=0|B=1) ≈ 0.0769, P(A=1|B=1) ≈ 0.9231
        assert!((result[0] - 0.04 / 0.52).abs() < 1e-4);
        assert!((result[1] - 0.48 / 0.52).abs() < 1e-4);
    }
}
