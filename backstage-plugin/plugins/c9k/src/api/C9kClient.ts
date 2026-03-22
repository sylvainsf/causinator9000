import {
  createApiRef,
  DiscoveryApi,
  FetchApi,
} from '@backstage/core-plugin-api';
import { Diagnosis, AlertGroup, EngineHealth } from './types';

export interface C9kApi {
  getHealth(): Promise<EngineHealth>;
  getDiagnosis(target: string): Promise<Diagnosis>;
  getAllDiagnoses(): Promise<Diagnosis[]>;
  getAlertGroups(): Promise<AlertGroup[]>;
}

export const c9kApiRef = createApiRef<C9kApi>({
  id: 'plugin.c9k.api',
});

export class C9kClient implements C9kApi {
  private readonly discoveryApi: DiscoveryApi;
  private readonly fetchApi: FetchApi;

  constructor(options: { discoveryApi: DiscoveryApi; fetchApi: FetchApi }) {
    this.discoveryApi = options.discoveryApi;
    this.fetchApi = options.fetchApi;
  }

  private async baseUrl(): Promise<string> {
    return this.discoveryApi.getBaseUrl('c9k');
  }

  private async get<T>(path: string): Promise<T> {
    const base = await this.baseUrl();
    const response = await this.fetchApi.fetch(`${base}${path}`);
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`C9K API error (${response.status}): ${text}`);
    }
    return response.json() as Promise<T>;
  }

  async getHealth(): Promise<EngineHealth> {
    return this.get('/health');
  }

  async getDiagnosis(target: string): Promise<Diagnosis> {
    return this.get(`/diagnosis?target=${encodeURIComponent(target)}`);
  }

  async getAllDiagnoses(): Promise<Diagnosis[]> {
    return this.get('/diagnosis/all');
  }

  async getAlertGroups(): Promise<AlertGroup[]> {
    return this.get('/alert-groups');
  }
}
