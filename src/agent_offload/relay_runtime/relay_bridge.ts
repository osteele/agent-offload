/**
 * Relay bridge: drives a remote OMP agent over SSH through the maintained
 * RPC client while every tool call executes locally against the live
 * project root.
 *
 * stdin  — one newline-terminated request (agent-offload.relay-request/v1),
 *          kept open as the attached parent's lifeline.
 * stdout — NDJSON envelope frames (agent-offload.relay-bridge/v1) and
 *          nothing else; any stray SDK output here makes the conductor
 *          refuse the stream, which is the intended failure direction.
 * stderr — human diagnostics.
 *
 * Fail-closed rule: if the server does not confirm registration of exactly
 * the three host tools are registered, the bridge emits an error frame and
 * exits nonzero instead of degrading to a remotely-executed fallback.
 */
import { defineRpcClientTool, RpcClient } from "@oh-my-pi/pi-coding-agent";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { readRequestLine, watchLifeline } from "./lifeline.js";
import {
	assertToolArgs,
	ConfinementError,
	globBound,
	grepBound,
	readBound,
	type RelayBounds,
	type ToolOutput,
} from "./confinement.js";
import { failedTurnMessage } from "./event_failure.js";
import { buildRemoteArgv, REMOTE_DISABLE_FLAGS, shellQuote } from "./remote_argv.js";

const HOST_TOOLS = ["review_read", "review_glob", "review_grep"] as const;
const RELAY_PROVIDER = "anthropic";
const RELAY_MODEL = "anthropic/claude-opus-5-5";
const BRIDGE_SCHEMA = "agent-offload.relay-bridge/v1";
const REQUEST_SCHEMA = "agent-offload.relay-request/v1";
const SDK_VERSION = "18.2.10";
const SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"];

const REMOTE_DISABLE_CONFIG = `goal:
  enabled: false
tools:
  xdev: false
secrets:
  enabled: false
includeWorkspaceTree: false
externalThinking: false
`;

interface RelayRequest {
	project_root: string;
	prompt: string;
	system_prompt?: string;
	host_target: string;
	provider?: string;
	model?: string;
	timeout_seconds: number;
	bounds: RelayBounds;
}

interface ServedPath {
	path: string;
	sha256: string;
	bytes: number;
	truncated: boolean;
}

function sha256(text: string): string {
	return createHash("sha256").update(text).digest("hex");
}

// A dead parent may close stdout while the bridge is cancelling.
process.stdout.on("error", () => {});
let writeChain: Promise<void> = Promise.resolve();

function writeFrame(frame: Record<string, unknown>): Promise<void> {
	// Serialized: event frames are fire-and-forget, so a bare
	// process.stdout.write could interleave with the awaited result frame.
	const next = writeChain.then(
		() =>
			new Promise<void>((resolvePromise, rejectPromise) => {
				process.stdout.write(`${JSON.stringify(frame)}\n`, (error) =>
					error ? rejectPromise(error) : resolvePromise(),
				);
			}),
	);
	writeChain = next.catch(() => {});
	return next;
}

function diagnostic(message: string): void {
	process.stderr.write(`relay-bridge: ${message}\n`);
}

function readRequest(parsed: unknown): RelayRequest {
	if (!parsed || typeof parsed !== "object") throw new Error("request must be an object");
	const raw = parsed as Record<string, unknown>;
	if (raw.schema_version !== REQUEST_SCHEMA) {
		throw new Error(`request.schema_version must be ${REQUEST_SCHEMA}`);
	}
	for (const key of ["project_root", "prompt", "host_target"] as const) {
		if (typeof raw[key] !== "string" || (raw[key] as string).length === 0) {
			throw new Error(`request.${key} must be a non-empty string`);
		}
	}
	const bounds = raw.bounds as RelayBounds | undefined;
	if (!bounds || bounds.maxReadBytes <= 0 || bounds.maxGrepMatches <= 0) {
		throw new Error("request.bounds missing or empty");
	}
	// Defense in depth behind the conductor's pin: the grounded lane serves
	// exactly one selector, so any drift arrives here only as a bug.
	if (
		(raw.provider !== undefined && raw.provider !== RELAY_PROVIDER) ||
		(raw.model !== undefined && raw.model !== RELAY_MODEL)
	) {
		throw new Error(
			`fail-closed: grounded relay is pinned to ${RELAY_PROVIDER}/${RELAY_MODEL}`,
		);
	}
	return raw as unknown as RelayRequest;
}

/** Run a short command through the SSH target and return its stdout. */
function sshCapture(host: string, remoteCommand: string, signal: AbortSignal): Promise<string> {
	return new Promise((resolve, reject) => {
		execFile(
			"ssh", [...SSH_OPTS, host, remoteCommand],
			{ encoding: "utf8", timeout: 30_000, signal },
			(error, stdout, stderr) => {
				if (error) reject(new Error(`ssh preflight failed: ${(stderr || error.message).slice(0, 400)}`));
				else resolve(stdout.trim());
			},
		);
	});
}

function buildHostTools(request: RelayRequest) {
	const bounds = request.bounds;
	// The RPC host-tool boundary requires the AgentToolResult shape: content
	// must be the typed block array, not a bare string.
	const asToolResult = (output: ToolOutput) => ({
		content: [{ type: "text" as const, text: output.content }],
		details: output.details,
	});
	const refuse = (error: unknown) => {
		if (error instanceof ConfinementError) {
			return {
				content: [{ type: "text" as const, text: `REFUSED: ${error.message}` }],
				isError: true,
			};
		}
		throw error;
	};
	return [
		defineRpcClientTool({
			name: "review_read",
			label: "Read local project file",
			description:
				"Read one UTF-8 text file from the live local project on the user's laptop. Paths resolve inside the project root only; URL schemes, devices, symlinks out of the root, binary, and oversized files are refused.",
			parameters: {
				type: "object",
				properties: {
					path: { type: "string", description: "Path relative to the project root" },
				},
				required: ["path"],
				additionalProperties: false,
			},
			execute: async (params) => {
				try {
					assertToolArgs("review_read", params, ["path"], bounds);
				} catch (error) {
					diagnostic(`review_read args refused: ${(error as Error).message}`);
					return refuse(error);
				}
				try {
					const output = await readBound(request.project_root, String(params.path), bounds);
					diagnostic("review_read completed");
					return asToolResult(output);
				} catch (error) {
					diagnostic(`review_read refused: ${(error as Error).message}`);
					return refuse(error);
				}
			},
		}),
		defineRpcClientTool({
			name: "review_glob",
			label: "List local project files",
			description:
				"List textual files under the live local project whose paths contain the given relative fragment. Bounded; binary and oversized files are skipped and reported.",
			parameters: {
				type: "object",
				properties: {
					pattern: { type: "string", description: "Relative path fragment, may include *" },
				},
				required: ["pattern"],
				additionalProperties: false,
			},
			execute: async (params) => {
				try {
					assertToolArgs("review_glob", params, ["pattern"], bounds);
				} catch (error) {
					diagnostic(`review_glob args refused: ${(error as Error).message}`);
					return refuse(error);
				}
				try {
					return asToolResult(
						await globBound(request.project_root, String(params.pattern), bounds),
					);
				} catch (error) {
					return refuse(error);
				}
			},
		}),
		defineRpcClientTool({
			name: "review_grep",
			label: "Search local project files",
			description:
				"Literal substring search across textual files of the live local project. Bounded in files, matches, and bytes.",
			parameters: {
				type: "object",
				properties: {
					pattern: { type: "string", description: "Literal text to find" },
				},
				required: ["pattern"],
				additionalProperties: false,
			},
			execute: async (params) => {
				try {
					assertToolArgs("review_grep", params, ["pattern"], bounds);
				} catch (error) {
					diagnostic(`review_grep args refused: ${(error as Error).message}`);
					return refuse(error);
				}
				try {
					return asToolResult(
						await grepBound(request.project_root, String(params.pattern), bounds),
					);
				} catch (error) {
					return refuse(error);
				}
			},
		}),
	];
}

async function main(): Promise<number> {
	const parsed = await readRequestLine(process.stdin);
	let request: RelayRequest;
	try {
		request = readRequest(parsed);
	} catch (error) {
		watchLifeline(process.stdin, () => {})();
		throw error;
	}
	const startedAt = Date.now() / 1000;
	const events: unknown[] = [];
	let failure: string | null = null;
	let cancelled = false;
	let lastRemoteArgv = "";
	let client: RpcClient | null = null;
	const preflightAbort = new AbortController();
	let stopPromise: Promise<void> | null = null;
	let rejectCancellation: ((reason: Error) => void) | null = null;
	const cancellation = new Promise<never>((_resolve, reject) => {
		rejectCancellation = reject;
	});
	void cancellation.catch(() => {});
	const stopOnce = (): Promise<void> => {
		if (client === null) return Promise.resolve();
		if (stopPromise === null) {
			stopPromise = client.stop().catch(() => {});
		}
		return stopPromise;
	};
	const cancel = (reason: string) => {
		if (cancelled) return;
		cancelled = true;
		failure = reason;
		preflightAbort.abort();
		void stopOnce();
		rejectCancellation?.(new Error(reason));
	};
	const onSignal = () => cancel("cancelled locally");
	const disposeLifeline = watchLifeline(process.stdin, () => cancel("relay parent exited"));
	process.on("SIGINT", onSignal);
	process.on("SIGTERM", onSignal);
	process.on("SIGHUP", onSignal);
	try {
		return await runBridge();
	} finally {
		disposeLifeline();
		process.off("SIGINT", onSignal);
		process.off("SIGTERM", onSignal);
		process.off("SIGHUP", onSignal);
	}

	async function runBridge(): Promise<number> {
		// Preflight before anything that could start an agent: locate omp by the
		// remote shell's own PATH, capture host identity, and prepare a scratch
		// directory that contains no project context for the agent to discover.
		let ompBin = "";
		let ompVersion = "";
		let hostIdentity = "";
		let scratch = "";
		try {
			ompBin = await sshCapture(request.host_target, "command -v omp", preflightAbort.signal);
			ompVersion = await sshCapture(request.host_target, "omp --version", preflightAbort.signal);
			hostIdentity = await sshCapture(request.host_target, "uname -a", preflightAbort.signal);
			scratch = await sshCapture(
				request.host_target,
				'd=$(mktemp -d "${TMPDIR:-/tmp}/agent-offload-relay.XXXXXX") && printf "%s" "$d"',
				preflightAbort.signal,
			);
			await sshCapture(
				request.host_target,
				`cat > ${shellQuote(`${scratch}/relay-disable-overlay.yml`)} <<'RELAYEOF'\n${REMOTE_DISABLE_CONFIG}RELAYEOF`,
				preflightAbort.signal,
			);
		} catch (error) {
			if (!cancelled) throw error;
			await writeFrame({
				frame: "result", schema_version: BRIDGE_SCHEMA,
				result: {
					sdk_version: SDK_VERSION, omp_version: ompVersion,
					host_target: request.host_target, host_identity: hostIdentity,
					remote_argv: lastRemoteArgv, remote_scratch: scratch,
					registered_tools: [], session_id: null, session_file: null,
					model: null, started_at: startedAt, ended_at: Date.now() / 1000,
					failure, cancelled, events, stats: null,
					prompt_sha256: sha256(request.system_prompt
						? `<project-context>\n${request.system_prompt}\n</project-context>\n\n${request.prompt}`
						: request.prompt),
					bounds: request.bounds, final_response: null,
				},
			}).catch(() => {});
			return 4;
		}

		const hostTools = buildHostTools(request);
		const rpc = new RpcClient({
			customTools: hostTools,
			provider: request.provider,
			model: request.model,
			args: [
				...REMOTE_DISABLE_FLAGS,
				"--config",
				"relay-disable-overlay.yml",
			],
			command: (agentArgs: string[]) => {
				lastRemoteArgv = buildRemoteArgv(scratch, ompBin, agentArgs);
				return ["ssh", ...SSH_OPTS, request.host_target, lastRemoteArgv];
			},
		});
		client = rpc;
		let rejectTurnFailure: ((reason?: unknown) => void) | null = null;
		let observedTurnFailure: string | null = null;
		const turnFailure = new Promise<never>((_resolve, reject) => {
			rejectTurnFailure = reject;
		});
		void turnFailure.catch(() => {});
		const prompt = request.system_prompt
			? `<project-context>\n${request.system_prompt}\n</project-context>\n\n${request.prompt}`
			: request.prompt;
		let registered: string[] = [];

		try {
			if (cancelled) throw new Error(failure ?? "cancelled locally");
			await Promise.race([rpc.start(), cancellation]);
			registered = await Promise.race([rpc.setCustomTools(hostTools), cancellation]);
			if ([...registered].sort().join(",") !== [...HOST_TOOLS].sort().join(",")) {
				throw new Error(
					`fail-closed: server registered host tools [${registered.join(", ")}]; expected exactly [${HOST_TOOLS.join(", ")}]`,
				);
			}
			await writeFrame({
				frame: "ready",
				schema_version: BRIDGE_SCHEMA,
				sdk_version: SDK_VERSION,
				omp_version: ompVersion,
				host_identity: hostIdentity,
				remote_argv: lastRemoteArgv,
				registered_tools: registered,
			});
			const unsubscribe = rpc.onEvent((event) => {
				events.push(event);
				const failureMessage = failedTurnMessage(event);
				if (failureMessage) observedTurnFailure = failureMessage;
				if (failureMessage && rejectTurnFailure) {
					const reject = rejectTurnFailure;
					rejectTurnFailure = null;
					reject(new Error(failureMessage));
				}
				void writeFrame({
					frame: "event",
					schema_version: BRIDGE_SCHEMA,
					event,
				}).catch(() => {});
			});
			try {
				await Promise.race([rpc.prompt(prompt), cancellation]);
				await Promise.race([
					rpc.waitForIdle(request.timeout_seconds * 1000), turnFailure, cancellation,
				]);
				if (observedTurnFailure) throw new Error(observedTurnFailure);
			} catch (error) {
				failure = cancelled ? failure : `prompt failed: ${(error as Error).message}`;
			}
			unsubscribe();

			const state = cancelled ? null : await Promise.race([rpc.getState(), cancellation]).catch(() => null);
			const stats = cancelled ? null : await Promise.race([rpc.getSessionStats(), cancellation]).catch(() => null);
			const finalResponse = cancelled
				? null : await Promise.race([rpc.getLastAssistantText(), cancellation]).catch(() => null);

			await writeFrame({
				frame: "result",
				schema_version: BRIDGE_SCHEMA,
				result: {
					sdk_version: SDK_VERSION,
					omp_version: ompVersion,
					host_target: request.host_target,
					host_identity: hostIdentity,
					remote_argv: lastRemoteArgv,
					remote_scratch: scratch,
					registered_tools: registered,
					session_id: state?.sessionId ?? null,
					session_file: state?.sessionFile ?? null,
					model: state?.model
						? { provider: state.model.provider, id: state.model.id }
						: null,
					started_at: startedAt,
					ended_at: Date.now() / 1000,
					failure,
					cancelled,
					events,
					stats,
					prompt_sha256: sha256(prompt),
					bounds: request.bounds,
					final_response: finalResponse ?? null,
				},
			}).catch(() => {});
			return failure === null ? 0 : 4;
		} catch (error) {
			if (cancelled) {
				await writeFrame({
					frame: "result",
					schema_version: BRIDGE_SCHEMA,
					result: {
						sdk_version: SDK_VERSION, omp_version: ompVersion,
						host_target: request.host_target, host_identity: hostIdentity,
						remote_argv: lastRemoteArgv, remote_scratch: scratch,
						registered_tools: registered, session_id: null, session_file: null,
						model: null, started_at: startedAt, ended_at: Date.now() / 1000,
						failure, cancelled, events, stats: null,
						prompt_sha256: sha256(prompt), bounds: request.bounds,
						final_response: null,
					},
				}).catch(() => {});
				return 4;
			}
			await writeFrame({
				frame: "error",
				schema_version: BRIDGE_SCHEMA,
				error: {
					message: (error as Error).message,
					stack: (error as Error).stack?.slice(-1500) ?? null,
					remote_argv: lastRemoteArgv,
					stderr_tail: rpc.getStderr().slice(-2000),
				},
			}).catch(() => {});
			diagnostic(`fatal: ${(error as Error).message}`);
			return 3;
		} finally {
			if (cancelled) void stopOnce();
			else await stopOnce();
		}
	}
}

process.exitCode = await main();
