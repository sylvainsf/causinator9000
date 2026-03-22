import {
  coreServices,
  createBackendPlugin,
} from '@backstage/backend-plugin-api';
import { createRouter } from './router';

export const c9kPlugin = createBackendPlugin({
  pluginId: 'c9k',
  register(reg) {
    reg.registerInit({
      deps: {
        logger: coreServices.logger,
        httpRouter: coreServices.httpRouter,
        config: coreServices.rootConfig,
      },
      async init({ logger, httpRouter, config }) {
        const baseUrl =
          config.getOptionalString('c9k.baseUrl') ??
          'http://localhost:8080';

        logger.info(`C9K backend plugin starting, engine at ${baseUrl}`);

        const router = createRouter({ logger, baseUrl });
        httpRouter.use(router);
      },
    });
  },
});
