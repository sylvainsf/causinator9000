# CPT Calibration Report

## Data Summary
- Total runs collected: 1118
- Failed runs: 162
- Successful runs: 956
- Unique repos: 18
- Unique SHAs: 291

## Per-Signal Background Rates

| Signal Type | Background Rate | 95% CI | N |
|-------------|----------------|--------|---|
| ImagePullError | 0.004 | [0.001, 0.008] | 1118 |
| LintFailure | 0.002 | [0.000, 0.004] | 1118 |
| ScorecardFailure | 0.002 | [0.000, 0.004] | 1118 |
| TestFailure | 0.138 | [0.119, 0.158] | 1118 |

## Per-Mutation Hit Rates

| Mutation | Signal | P(sig|mut) | P(sig|¬mut) | LR | N_mut | N_bg | Source |
|----------|--------|------------|-------------|-----|-------|------|--------|
| CodeChange | TestFailure | 0.136 | 0.144 | 0.9× | 840 | 278 | ✓ |
| DepMinorBump | TestFailure | 0.300 | 0.125 | 2.4× | 80 | 1038 | ✓ |
| DependencyUpdate | LintFailure | 0.013 | 0.001 | 12.8× | 78 | 1040 | ⚠️ |
| DependencyUpdate | TestFailure | 0.115 | 0.139 | 0.8× | 78 | 1040 | ✓ |
| Release | TestFailure | 0.069 | 0.144 | 0.5× | 87 | 1031 | ✓ |
| Revert | TestFailure | 0.071 | 0.139 | 0.5× | 14 | 1104 | ⚠️ |

## Validation

- Spread: 65.5pp
- Min confidence: 33.3%
- Max confidence: 98.8%
- Valid: ✓ (Spread 65.5pp (≥ 30pp target))
