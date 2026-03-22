import {
  createApiFactory,
  createPlugin,
  createRouteRef,
  createRoutableExtension,
  discoveryApiRef,
  fetchApiRef,
} from '@backstage/core-plugin-api';
import { c9kApiRef, C9kClient } from './api';

export const rootRouteRef = createRouteRef({
  id: 'c9k',
});

export const c9kPlugin = createPlugin({
  id: 'c9k',
  routes: {
    root: rootRouteRef,
  },
  apis: [
    createApiFactory({
      api: c9kApiRef,
      deps: {
        discoveryApi: discoveryApiRef,
        fetchApi: fetchApiRef,
      },
      factory: ({ discoveryApi, fetchApi }) =>
        new C9kClient({ discoveryApi, fetchApi }),
    }),
  ],
});

/** Entity page tab showing C9K root-cause diagnoses. */
export const EntityC9kDiagnosisTab = c9kPlugin.provide(
  createRoutableExtension({
    name: 'EntityC9kDiagnosisTab',
    component: () =>
      import('./components/DiagnosisTab').then(m => m.DiagnosisTab),
    mountPoint: rootRouteRef,
  }),
);
