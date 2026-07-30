import { LoggerService } from '@backstage/backend-plugin-api';
import Router from 'express-promise-router';
import fetch from 'node-fetch';
import { Request, Response } from 'express';

interface RouterOptions {
  logger: LoggerService;
  baseUrl: string;
}

export function createRouter({ logger, baseUrl }: RouterOptions) {
  const router = Router();

  // Proxy helper, forwards GET requests to the C9K engine.
  async function proxy(
    enginePath: string,
    req: Request,
    res: Response,
  ) {
    const qs = new URLSearchParams(
      req.query as Record<string, string>,
    ).toString();
    const url = `${baseUrl}${enginePath}${qs ? `?${qs}` : ''}`;

    try {
      const upstream = await fetch(url, { timeout: 15_000 });
      const body = await upstream.json();
      res.status(upstream.status).json(body);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      logger.error(`C9K proxy error: ${message}`);
      res.status(502).json({ error: 'C9K engine unreachable', detail: message });
    }
  }

  // ── Read-only endpoints exposed to the Backstage frontend ──────────

  router.get('/health', (req, res) => proxy('/api/health', req, res));

  router.get('/diagnosis', (req, res) =>
    proxy('/api/diagnosis', req, res),
  );

  router.get('/diagnosis/all', (req, res) =>
    proxy('/api/diagnosis/all', req, res),
  );

  router.get('/alert-groups', (req, res) =>
    proxy('/api/alert-groups', req, res),
  );

  router.get('/neighborhood', (req, res) =>
    proxy('/api/neighborhood', req, res),
  );

  return router;
}
