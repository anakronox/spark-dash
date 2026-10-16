/* Each agent's setup for the dashboard's tools (roadmap BB2): printed as JSON
 * for tests/test_mcp_clients_js.py, which parses every block the way its
 * agent would (YAML, TOML, JSON, a shell command).
 */

import { CLIENTS, clientOr, setupFor } from '../../frontend/src/lib/mcpClients.ts';

const URL = 'http://monitor.example:8080/mcp';
const KEY = 'sdk-abc_DEF-123';

const out = {
  setups: Object.fromEntries(CLIENTS.map((c) => [c.id, setupFor(c.id, URL, KEY)])),
  fallback: [clientOr(null), clientOr('gone'), clientOr('hermes')],
};
console.log(JSON.stringify(out));
