// SportsBrain Signals Worker
// GET  /signals.json  → reads from KV (public)
// POST /signals       → writes to KV (requires Bearer token)
export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: cors() });
    }

    if (request.method === 'GET') {
      const data = await env.SIGNALS.get('signals_json');
      if (!data) {
        return new Response('{"error":"no data yet"}', {
          status: 404,
          headers: { 'Content-Type': 'application/json', ...cors() },
        });
      }
      return new Response(data, {
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-cache, max-age=0',
          ...cors(),
        },
      });
    }

    if (request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (auth !== `Bearer ${env.API_TOKEN}`) {
        return new Response('Unauthorized', { status: 401 });
      }
      const body = await request.text();
      try {
        JSON.parse(body);
      } catch {
        return new Response('Invalid JSON', { status: 400 });
      }
      await env.SIGNALS.put('signals_json', body);
      return new Response('OK', { headers: cors() });
    }

    return new Response('Not Found', { status: 404 });
  },
};

function cors() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type',
  };
}
