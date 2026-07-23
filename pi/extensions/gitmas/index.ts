import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";

const HOME = homedir();

// Real Pi session id, captured on session_start — flows to the memory bridge
// (env) and to ~/.gitmas/session-map/<pid>.json so any sibling MCP process
// can resolve the exact session too.
let currentSessionId = "";

function publishSessionMap(sid: string, cwd: string) {
	try {
		const dir = `${HOME}/.gitmas/session-map`;
		mkdirSync(dir, { recursive: true });
		writeFileSync(
			`${dir}/${process.pid}.json`,
			JSON.stringify({ session_id: sid, cwd, ts: new Date().toISOString() }) + "\n",
		);
	} catch {
		// best effort — memory falls back to transcript-mtime guessing
	}
}
const PYTHON = process.env.GITMAS_MCP_PYTHON || `${HOME}/.gitmas/memory-server/.venv/bin/python`;
const BRIDGE = process.env.PI_GITMAS_BRIDGE || `${HOME}/.pi/agent/extensions/gitmas/gitmas_bridge.py`;

type Params = Record<string, unknown>;

function runGitmas(toolName: string, params: Params): Promise<string> {
	return new Promise((resolve, reject) => {
		const child = spawn(PYTHON, [BRIDGE, toolName], {
			env: {
				...process.env,
				...(currentSessionId ? { GITMAS_SESSION_ID: currentSessionId } : {}),
				MEMORY_DEVICE_ROLE: process.env.MEMORY_DEVICE_ROLE || "mac",
				MEMORY_USER_ID: process.env.MEMORY_USER_ID || process.env.GITMAS_USER_ID || "karl-mac",
				MEMORY_SOURCE: process.env.MEMORY_SOURCE || process.env.GITMAS_SOURCE || "pi-agent",
				MEMORY_AGENT_SOURCE: process.env.MEMORY_AGENT_SOURCE || process.env.GITMAS_AGENT_SOURCE || "pi-agent",
				MEMORY_AGENT_ID: process.env.MEMORY_AGENT_ID || process.env.GITMAS_AGENT_ID || "mac-pi-agent",
			},
			stdio: ["pipe", "pipe", "pipe"],
		});

		let stdout = "";
		let stderr = "";
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stdout.on("data", (chunk) => {
			stdout += chunk;
		});
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});
		child.on("error", reject);
		child.on("close", (code) => {
			if (code === 0) {
				resolve(stdout.trim() || "(no output)");
			} else {
				reject(new Error((stderr || stdout || `Gitmas bridge exited ${code}`).trim()));
			}
		});
		child.stdin.end(JSON.stringify(params));
	});
}

function textResult(text: string, details: Params = {}) {
	return {
		content: [{ type: "text" as const, text }],
		details,
	};
}

function registerGitmasTool(
	pi: ExtensionAPI,
	name: string,
	label: string,
	description: string,
	parameters: unknown,
) {
	pi.registerTool({
		name,
		label,
		description,
		parameters,
		async execute(_toolCallId, params) {
			const text = await runGitmas(name, params as Params);
			return textResult(text, { gitmasTool: name });
		},
	});
}

const OptionalString = Type.Optional(Type.String());
const OptionalBoolean = Type.Optional(Type.Boolean());
const OptionalNumber = Type.Optional(Type.Number());
const StringArray = Type.Optional(Type.Array(Type.String()));

export default function gitmasExtension(pi: ExtensionAPI) {
	pi.on("session_start", (_event: unknown, ctx: any) => {
		try {
			const sid = ctx?.sessionManager?.getSessionId?.() || "";
			if (sid) {
				currentSessionId = String(sid);
				publishSessionMap(currentSessionId, ctx?.sessionManager?.getCwd?.() || process.cwd());
			}
		} catch {
			// session id stays empty — bridge falls back to server-side resolution
		}
	});

	registerGitmasTool(pi, "memory_store", "Gitmas Store Memory", "Store a new Gitmas memory with scope control.", Type.Object({
		content: Type.String({ description: "Memory content" }),
		description: Type.String({ description: "One-line description" }),
		type: Type.String({ description: "Memory type: user, feedback, project, or reference" }),
		tags: StringArray,
		scope: Type.Optional(Type.String({ default: "global", description: "Scope: global, project, agent, or user" })),
		project: OptionalString,
		agent: OptionalString,
	}));

	registerGitmasTool(pi, "memory_search", "Gitmas Search Memory", "Search Gitmas memories semantically with optional filters.", Type.Object({
		query: Type.String(),
		limit: OptionalNumber,
		type: OptionalString,
		scope: OptionalString,
		project: OptionalString,
		agent: OptionalString,
		since: OptionalString,
		until: OptionalString,
		chronological: OptionalBoolean,
		debug: OptionalBoolean,
	}));

	registerGitmasTool(pi, "memory_list", "Gitmas List Memories", "List Gitmas memories by ID, type, scope, and description.", Type.Object({
		type: OptionalString,
		scope: OptionalString,
		project: OptionalString,
	}));

	registerGitmasTool(pi, "memory_identity", "Gitmas Memory Identity", "Show the active Gitmas identity for Pi.", Type.Object({}));

	registerGitmasTool(pi, "memory_update", "Gitmas Update Memory", "Update an existing Gitmas memory.", Type.Object({
		id: Type.String(),
		content: Type.String(),
		description: OptionalString,
	}));

	registerGitmasTool(pi, "memory_delete", "Gitmas Delete Memory", "Delete a Gitmas memory by ID.", Type.Object({
		id: Type.String(),
	}));

	registerGitmasTool(pi, "memory_server_info", "Gitmas Server Info", "Show Gitmas memory server/tool metadata.", Type.Object({}));

	registerGitmasTool(pi, "memory_mark_helpful", "Gitmas Mark Helpful", "Mark a memory as helpful to improve future ranking.", Type.Object({
		id: Type.String(),
	}));

	registerGitmasTool(pi, "memory_quality_report", "Gitmas Quality Report", "Show recent Gitmas search quality metrics.", Type.Object({
		days: OptionalNumber,
	}));

	registerGitmasTool(pi, "memory_entity_add", "Gitmas Add Entity", "Register or update an entity and aliases for query expansion.", Type.Object({
		canonical_name: Type.String(),
		aliases: StringArray,
		entity_type: Type.Optional(Type.String()),
	}));

	registerGitmasTool(pi, "memory_entity_list", "Gitmas List Entities", "List registered Gitmas query-expansion entities.", Type.Object({}));

	registerGitmasTool(pi, "memory_mark_canonical", "Gitmas Mark Canonical", "Add or remove the canonical tag for a memory.", Type.Object({
		id: Type.String(),
		canonical: OptionalBoolean,
	}));

	registerGitmasTool(pi, "memory_mark_irrelevant", "Gitmas Mark Irrelevant", "Mark a memory as irrelevant to lower future ranking.", Type.Object({
		id: Type.String(),
	}));

	registerGitmasTool(pi, "agent_list", "Gitmas List Agents", "List registered Gitmas MAS agents.", Type.Object({}));

	registerGitmasTool(pi, "project_list", "Gitmas List Projects", "List registered Gitmas MAS projects.", Type.Object({}));

	registerGitmasTool(pi, "agent_send_message", "Gitmas Send Agent Message", "Send an inter-agent Gitmas message.", Type.Object({
		from_agent: Type.String(),
		content: Type.String(),
		to_agent: OptionalString,
		project: OptionalString,
		message_type: Type.Optional(Type.String()),
		priority: Type.Optional(Type.String()),
	}));

	registerGitmasTool(pi, "agent_messages", "Gitmas Agent Messages", "View Gitmas messages for an agent.", Type.Object({
		agent_id: Type.Optional(Type.String()),
		project: OptionalString,
		include_read: OptionalBoolean,
		limit: OptionalNumber,
	}));

	registerGitmasTool(pi, "session_list", "Gitmas List Sessions", "List Claude Code sessions synced to Gitmas.", Type.Object({
		device_id: OptionalString,
		days: OptionalNumber,
		limit: OptionalNumber,
	}));

	registerGitmasTool(pi, "session_read", "Gitmas Read Session", "Read a synced Claude Code session from Gitmas.", Type.Object({
		session_id: Type.String(),
		device_id: OptionalString,
		project_dir: OptionalString,
	}));
}
