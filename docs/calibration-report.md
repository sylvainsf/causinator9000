# CPT Calibration Report

## Data Summary
- Total runs collected: 640
- Failed runs: 89
- Successful runs: 551
- Unique repos: 21
- Unique SHAs: 203

## Per-Signal Background Rates

| Signal Type | Background Rate | 95% CI | N |
|-------------|----------------|--------|---|
| ImagePullError | 0.005 | [0.000, 0.011] | 640 |
| LintFailure | 0.006 | [0.002, 0.013] | 640 |
| TestFailure | 0.127 | [0.103, 0.152] | 640 |
| UnitTestFailure | 0.002 | [0.000, 0.005] | 640 |

## Per-Mutation Hit Rates

| Mutation | Signal | P(sig|mut) | P(sig|¬mut) | LR | N_mut | N_bg | Source |
|----------|--------|------------|-------------|-----|-------|------|--------|
| CodeChange | TestFailure | 0.137 | 0.057 | 2.4× | 553 | 87 | ✓ |
| DepGroupUpdate | TestFailure | 1.000 | 0.125 | 8.0× | 1 | 639 | ⚠️ |
| DepMinorBump | TestFailure | 0.059 | 0.128 | 0.5× | 17 | 623 | ⚠️ |
| Release | TestFailure | 0.048 | 0.132 | 0.4× | 42 | 598 | ✓ |
| Revert | TestFailure | 0.071 | 0.128 | 0.6× | 14 | 626 | ⚠️ |

## Validation

- Spread: 71.1pp
- Min confidence: 27.8%
- Max confidence: 98.8%
- Valid: ✓ (Spread 71.1pp (≥ 30pp target))
