/** How each common agent is pointed at the dashboard's tools (roadmap BB2).
 *
 * Settings shows the endpoint and the key on their own, for any client, and
 * the exact command or config block for the agent chosen here. Formats as
 * each project documents them, checked 2026-10-16:
 *   Claude Code   `claude mcp add --transport http ... --header`
 *   Hermes Agent  ~/.hermes/config.yaml, mcp_servers.<name>.url / headers
 *   Codex CLI     ~/.codex/config.toml, [mcp_servers.<name>] url / http_headers
 *   Gemini CLI    `gemini mcp add --transport http ... --header`
 *   Cursor        ~/.cursor/mcp.json, mcpServers.<name>.url / headers
 *   VS Code       .vscode/mcp.json, servers.<name>.type "http" / url / headers
 *   OpenCode      opencode.json, mcp.<name>.type "remote" / url / headers
 */

export const SERVER_NAME = 'spark-dash';

export interface ClientSetup {
  /** Where it goes: a shell, or a file to add the block to. */
  where: string;
  text: string;
  /** What to do after, when there is something. */
  then?: string;
}

export const CLIENTS = [
  { id: 'claude-code', label: 'Claude Code' },
  { id: 'hermes', label: 'Hermes Agent' },
  { id: 'codex', label: 'Codex CLI' },
  { id: 'gemini', label: 'Gemini CLI' },
  { id: 'cursor', label: 'Cursor' },
  { id: 'vscode', label: 'VS Code' },
  { id: 'opencode', label: 'OpenCode' },
  { id: 'other', label: 'Other' },
] as const;

export type ClientId = (typeof CLIENTS)[number]['id'];

const json = (o: unknown) => JSON.stringify(o, null, 2);

export function setupFor(client: ClientId, url: string, key: string): ClientSetup {
  const bearer = `Bearer ${key}`;
  switch (client) {
    case 'claude-code':
      return {
        where: 'Run in a shell',
        text: `claude mcp add --transport http ${SERVER_NAME} ${url} \\\n  --header "Authorization: ${bearer}"`,
      };
    case 'hermes':
      return {
        where: 'Add to ~/.hermes/config.yaml',
        text: `mcp_servers:\n  ${SERVER_NAME}:\n    url: "${url}"\n    headers:\n      Authorization: "${bearer}"`,
        then: 'Then /reload-mcp in a session, or start a new one.',
      };
    case 'codex':
      return {
        where: 'Add to ~/.codex/config.toml',
        text: `[mcp_servers.${SERVER_NAME}]\nurl = "${url}"\nhttp_headers = { "Authorization" = "${bearer}" }`,
      };
    case 'gemini':
      return {
        where: 'Run in a shell',
        text: `gemini mcp add --transport http ${SERVER_NAME} ${url} \\\n  --header "Authorization: ${bearer}"`,
      };
    case 'cursor':
      return {
        where: 'Add to ~/.cursor/mcp.json',
        text: json({ mcpServers: { [SERVER_NAME]: { url, headers: { Authorization: bearer } } } }),
      };
    case 'vscode':
      return {
        where: 'Add to .vscode/mcp.json',
        text: json({ servers: { [SERVER_NAME]: { type: 'http', url, headers: { Authorization: bearer } } } }),
      };
    case 'opencode':
      return {
        where: 'Add to opencode.json',
        text: json({
          mcp: { [SERVER_NAME]: { type: 'remote', url, oauth: false, headers: { Authorization: bearer } } },
        }),
      };
    case 'other':
      return {
        where: 'Any client that speaks MCP over streamable HTTP',
        text: `URL     ${url}\nHeader  Authorization: ${bearer}`,
      };
  }
}

/** The stored choice, or Claude Code; a stale or unknown one falls back. */
export function clientOr(saved: string | null): ClientId {
  return CLIENTS.some((c) => c.id === saved) ? (saved as ClientId) : 'claude-code';
}
