/** Mirrors the C9K engine's DiagnosisResponse. */
export interface Diagnosis {
  target_node: string;
  confidence: number;
  root_cause: string | null;
  causal_path: string[];
  competing_causes: [string, number][];
  timestamp: string;
}

/** Mirrors the C9K engine's AlertGroup. */
export interface AlertGroup {
  root_cause: string;
  confidence: number;
  members: string[];
  mutation_type?: string;
}

/** Mirrors the C9K engine's HealthResponse. */
export interface EngineHealth {
  status: string;
  version: string;
  nodes: number;
  edges: number;
  active_mutations: number;
  active_signals: number;
}

/** Annotation keys used on Backstage catalog entities. */
export const C9K_REPO_ANNOTATION = 'c9k/repo';
export const C9K_NODE_ANNOTATION = 'c9k/node-id';
