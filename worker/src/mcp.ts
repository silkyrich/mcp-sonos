/**
 * Minimal, stateless MCP server over Streamable HTTP.
 *
 * Rather than depend on a Durable-Object-based agent, this implements the
 * JSON-RPC subset Claude's custom connector needs (initialize, tools/list,
 * tools/call, ping) directly. Each POST is a self-contained request answered
 * with a single application/json JSON-RPC response — no session state, which
 * suits a Worker and survives cold starts.
 */

import { Bridge, BridgeConfig, BridgeError } from "./bridge";
import { TOOLS, TOOLS_BY_NAME, type Speech } from "./tools";
import { log, logError } from "./log";

const PROTOCOL_VERSION = "2025-06-18";
const SERVER_INFO = { name: "mcp-sonos", version: "0.1.0" };

interface JsonRpcRequest {
  jsonrpc: "2.0";
  id?: string | number | null;
  method: string;
  params?: any;
}

function result(id: any, res: unknown) {
  return { jsonrpc: "2.0", id, result: res };
}
function error(id: any, code: number, message: string) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

async function dispatch(req: JsonRpcRequest, cfg: BridgeConfig, speech: Speech): Promise<object | null> {
  switch (req.method) {
    case "initialize":
      return result(req.id, {
        protocolVersion: req.params?.protocolVersion ?? PROTOCOL_VERSION,
        capabilities: { tools: {} },
        serverInfo: SERVER_INFO,
      });

    // Notifications (no id / no response expected)
    case "notifications/initialized":
    case "notifications/cancelled":
      return null;

    case "ping":
      return result(req.id, {});

    case "tools/list":
      return result(req.id, {
        tools: TOOLS.map((t) => ({
          name: t.name,
          description: t.description,
          inputSchema: t.inputSchema,
          ...(t.annotations ? { annotations: t.annotations } : {}),
        })),
      });

    case "tools/call": {
      const name = req.params?.name;
      const tool = TOOLS_BY_NAME.get(name);
      if (!tool) {
        logError("mcp.tool.unknown", { tool: name });
        return error(req.id, -32602, `Unknown tool: ${name}`);
      }
      const bridge = new Bridge(cfg);
      const started = Date.now();
      try {
        const data = await tool.handler(bridge, req.params?.arguments ?? {}, speech);
        log("mcp.tool.ok", { tool: name, ms: Date.now() - started });
        return result(req.id, {
          content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
        });
      } catch (e) {
        const isBridge = e instanceof BridgeError;
        logError("mcp.tool.failed", {
          tool: name,
          ms: Date.now() - started,
          status: isBridge ? (e as BridgeError).status : undefined,
          reason: (e as Error).message,
        });
        const msg = isBridge
          ? `Sonos bridge error (${(e as BridgeError).status}): ${(e as Error).message}`
          : `Tool error: ${(e as Error).message}`;
        return result(req.id, {
          content: [{ type: "text", text: msg }],
          isError: true,
        });
      }
    }

    default:
      return error(req.id, -32601, `Method not found: ${req.method}`);
  }
}

/**
 * Handle one Streamable-HTTP POST. Accepts a single JSON-RPC request or a
 * batch array; returns application/json. Notifications yield 202 with no body.
 */
export async function handleMcp(request: Request, cfg: BridgeConfig, speech: Speech): Promise<Response> {
  if (request.method === "GET") {
    // No server-initiated SSE stream in this stateless design.
    return new Response("Method Not Allowed", { status: 405 });
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  let payload: JsonRpcRequest | JsonRpcRequest[];
  try {
    payload = await request.json();
  } catch {
    return Response.json(error(null, -32700, "Parse error"), { status: 400 });
  }

  const batch = Array.isArray(payload);
  const reqs: JsonRpcRequest[] = batch ? (payload as JsonRpcRequest[]) : [payload as JsonRpcRequest];
  const responses = (await Promise.all(reqs.map((r) => dispatch(r, cfg, speech)))).filter(
    (r): r is object => r !== null,
  );

  if (responses.length === 0) {
    // Only notifications — nothing to return.
    return new Response(null, { status: 202 });
  }
  return Response.json(batch ? responses : responses[0]);
}
