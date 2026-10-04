/**
 * Project-root confinement for locally executed relay host tools.
 *
 * Every path the remote agent names is treated as hostile: URL or device
 * schemes are rejected, parent traversal collapses against the root, and a
 * symlink anywhere on the resolved path may not leave the root. Results are
 * bounded — by bytes, matches, file count, and total traversal — and each
 * served file carries a content digest so the evidence stream records what
 * the model actually saw, not what a later revision of the live tree
 * contains. Text is decoded strictly: invalid UTF-8 is a refusal, never a
 * silent replacement, so served text is always exactly what the bytes said.
 */
import { createHash } from "node:crypto";
import { readFile, readdir, realpath, stat } from "node:fs/promises";

export interface RelayBounds {
	maxReadBytes: number;
	maxMatchFiles: number;
	maxGrepMatches: number;
	maxToolResultBytes: number;
	/** Total files walked per glob/grep call, matching or not. */
	maxTraversedFiles: number;
	/** Encoded size cap for host-tool arguments, enforced before execution. */
	maxToolArgsBytes: number;
}

export class ConfinementError extends Error {}


export interface ServedPath {
	path: string;
	sha256: string;
	bytes: number;
	truncated: boolean;
}

export interface ToolOutput {
	content: string;
	details: {
		served_paths: ServedPath[];
		skipped: string[];
		truncated: boolean;
	};
}


/** Enforce the argument-size bound and reject unspecified properties before
 * any host tool executes or logs anything about its arguments. */
export function assertToolArgs(
	tool: string,
	params: unknown,
	allowedKeys: readonly string[],
	bounds: RelayBounds,
): void {
	const encoded = JSON.stringify(params) ?? "";
	if (Buffer.byteLength(encoded) > bounds.maxToolArgsBytes) {
		throw new ConfinementError(
			`tool arguments exceed the ${bounds.maxToolArgsBytes}-byte bound for ${tool}`,
		);
	}
	const keys = params === null || typeof params !== "object"
		? []
		: Object.keys(params as Record<string, unknown>);
	const unexpected = keys.filter((key) => !allowedKeys.includes(key));
	if (unexpected.length > 0) {
		throw new ConfinementError(`unexpected arguments for ${tool}: ${unexpected.join(", ")}`);
	}
}
function digest(bytes: Uint8Array): string {
	return createHash("sha256").update(bytes).digest("hex");
}

function rejectSchemes(userPath: string): void {
	// URL-like schemes, Windows drive letters, and device namespaces never
	// name a file inside the project root.
	if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(userPath)) {
		throw new ConfinementError(`unsupported path scheme: ${userPath}`);
	}
	if (userPath.startsWith("/dev/") || userPath === "/dev" || userPath.startsWith("/proc/")) {
		throw new ConfinementError(`refusing device or kernel path: ${userPath}`);
	}
}

/** Resolve userPath inside root, refusing every escape. Returns the real path. */
export async function confine(root: string, userPath: string): Promise<string> {
	if (typeof userPath !== "string" || userPath.length === 0) {
		throw new ConfinementError("path must be a non-empty string");
	}
	if (userPath.length > 4096) {
		throw new ConfinementError("path exceeds 4096 characters");
	}
	rejectSchemes(userPath);
	const rootReal = await realpath(root);
	let joined: string;
	try {
		joined = userPath.startsWith("/")
			? await realpath(userPath)
			: await realpath(`${rootReal}/${userPath}`);
	} catch {
		// A missing path is refused as confinement, not reported as a filesystem
		// error: the model must not learn which outside paths exist.
		throw new ConfinementError(`path does not exist inside the project root: ${userPath}`);
	}
	// The prefix check runs on real paths, so a symlink that resolves outside
	// the root fails here rather than leaking bytes.
	if (joined !== rootReal && !joined.startsWith(`${rootReal}/`)) {
		throw new ConfinementError(`resolved path escapes project root: ${userPath}`);
	}
	return joined;
}

function isTextual(bytes: Uint8Array): boolean {
	return !bytes.includes(0);
}

/** Strict UTF-8 decode: invalid sequences are a confinement refusal, not a
 * silent replacement, so served text is always exactly what the bytes said. */
function decodeUtf8(bytes: Uint8Array): string {
	try {
		return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
	} catch {
		throw new ConfinementError("file is not valid UTF-8");
	}
}

/** Truncate decoded UTF-8 text to an encoded-byte budget on a char boundary. */
function truncateUtf8(text: string, maxBytes: number): { text: string; truncated: boolean } {
	if (Buffer.byteLength(text) <= maxBytes) {
		return { text, truncated: false };
	}
	let lo = 0;
	let hi = text.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (Buffer.byteLength(text.slice(0, mid)) <= maxBytes) {
			lo = mid + 1;
		} else {
			hi = mid;
		}
	}
	let cut = lo - 1;
	// Never end on half a surrogate pair: drop a trailing high surrogate whose
	// low half was cut off, but keep complete pairs before it.
	if (cut > 0 && /[\uD800-\uDBFF]/.test(text[cut - 1] ?? "")) {
		cut -= 1;
	}
	const lastNewline = text.lastIndexOf("\n", cut);
	if (lastNewline > 0) {
		return { text: text.slice(0, lastNewline), truncated: true };
	}
	return { text: text.slice(0, cut), truncated: true };
}

/** Read one confined UTF-8 text file. */
export async function readBound(
	root: string,
	userPath: string,
	bounds: RelayBounds,
): Promise<ToolOutput> {
	const resolved = await confine(root, userPath);
	const info = await stat(resolved);
	if (!info.isFile()) {
		throw new ConfinementError(`not a regular file: ${userPath}`);
	}
	if (info.size > bounds.maxReadBytes * 4) {
		throw new ConfinementError(
			`file too large to inspect (${info.size} bytes): ${userPath}`,
		);
	}
	const bytes = await readFile(resolved);
	if (bytes.length > bounds.maxReadBytes) {
		throw new ConfinementError(
			`file exceeds the ${bounds.maxReadBytes}-byte read bound: ${userPath}`,
		);
	}
	if (!isTextual(bytes)) {
		throw new ConfinementError(`binary file is not readable: ${userPath}`);
	}
	const text = decodeUtf8(bytes);
	const { text: clipped, truncated } = truncateUtf8(text, bounds.maxToolResultBytes);
	const served: ServedPath = {
		path: resolved,
		sha256: digest(bytes),
		bytes: bytes.length,
		truncated,
	};
	return {
		content: clipped,
		details: { served_paths: [served], skipped: [], truncated },
	};
}

/** Translate a shell-style glob into a matcher over root-relative paths.
 * A `**` segment spans any number of directories, including none, so
 * `src/**\/*.ts` matches `src/a.ts`. Inside a segment, `**` is just `*`;
 * `*` and `?` never cross a `/`. */
export function globToMatcher(pattern: string): (relativePath: string) => boolean {
	const segments = pattern.split("/");
	let source = "";
	segments.forEach((segment, index) => {
		const last = index === segments.length - 1;
		if (segment === "**") {
			source += last ? ".*" : "(?:[^/]+/)*";
			return;
		}
		source += segment
			.replace(/[.+^${}()|[\]\\]/g, "\\$&")
			.replace(/\*+/g, "[^/]*")
			.replace(/\?/g, "[^/]");
		if (!last) source += "/";
	});
	const regex = new RegExp(`^${source}$`);
	return (relativePath) => regex.test(relativePath);
}

interface WalkState {
	visited: number;
	maxTraversed: number;
	stop: boolean;
}

type FileVisitor = (filePath: string) => Promise<void>;

/** Read a walked file only when it is within the read bound; null otherwise.
 * The size is checked before reading, so one large file in the tree cannot
 * pull its whole content into this process. */
async function readWithinBound(filePath: string, bounds: RelayBounds): Promise<Uint8Array | null> {
	if ((await stat(filePath)).size > bounds.maxReadBytes) return null;
	const bytes = await readFile(filePath);
	return bytes.length > bounds.maxReadBytes ? null : bytes;
}

/** Directories glob and grep never enter: version-control stores and
 * installed dependencies would spend the traversal bound before the
 * project's own files. review_read still reaches files inside them. */
const UNWALKED_DIRECTORIES = new Set([
	".git", ".jj", ".hg", ".svn", "node_modules", ".venv", "__pycache__",
]);

/** Bounded walk that never follows symlinks, skips UNWALKED_DIRECTORIES,
 * and stops after maxTraversed files — matching or not — so a nonmatching
 * tree terminates the call instead of funding an endless walk. */
async function walk(dir: string, state: WalkState, visit: FileVisitor): Promise<void> {
	if (state.stop) return;
	const entries = await readdir(dir, { withFileTypes: true });
	for (const entry of entries) {
		if (state.stop) return;
		if (entry.isSymbolicLink()) continue;
		if (entry.isDirectory() && UNWALKED_DIRECTORIES.has(entry.name)) continue;
		const child = `${dir}/${entry.name}`;
		if (entry.isDirectory()) {
			await walk(child, state, visit);
		} else if (entry.isFile()) {
			state.visited += 1;
			if (state.visited > state.maxTraversed) {
				state.stop = true;
				return;
			}
			await visit(child);
		}
	}
}

/** List files matching a glob under root (bounded). */
export async function globBound(
	root: string,
	pattern: string,
	bounds: RelayBounds,
): Promise<ToolOutput> {
	if (typeof pattern !== "string" || pattern.includes("..") || pattern.length > 512) {
		throw new ConfinementError("pattern must be a short relative glob without traversal");
	}
	const rootReal = await realpath(root);
	const glob = globToMatcher(pattern);
	const matched: ServedPath[] = [];
	const lines: string[] = [];
	const skipped: string[] = [];
	let outBytes = 0;
	let truncated = false;
	const state: WalkState = { visited: 0, maxTraversed: bounds.maxTraversedFiles, stop: false };
	await walk(rootReal, state, async (filePath) => {
		if (matched.length >= bounds.maxMatchFiles) {
			truncated = true;
			state.stop = true;
			return;
		}
		const relative = filePath.slice(rootReal.length + 1);
		if (!glob(relative)) return;
		const bytes = await readWithinBound(filePath, bounds);
		if (bytes === null || !isTextual(bytes)) {
			skipped.push(filePath);
			return;
		}
		const line = `${filePath} (${bytes.length}B)`;
		if (outBytes + Buffer.byteLength(line) + 1 > bounds.maxToolResultBytes) {
			truncated = true;
			state.stop = true;
			return;
		}
		outBytes += Buffer.byteLength(line) + 1;
		matched.push({
			path: filePath,
			sha256: digest(bytes),
			bytes: bytes.length,
			truncated: false,
		});
		lines.push(line);
	});
	if (state.stop) truncated = true;
	const listing = lines.length
		? lines.join("\n")
		: truncated
			? "bound reached before any matching file"
			: "no matching files";
	return {
		content: listing,
		details: { served_paths: matched, skipped, truncated },
	};
}

/** Grep a literal pattern across textual files under root (bounded). */
export async function grepBound(
	root: string,
	pattern: string,
	bounds: RelayBounds,
): Promise<ToolOutput> {
	if (typeof pattern !== "string" || pattern.length === 0 || pattern.length > 256) {
		throw new ConfinementError("pattern must be a 1-256 character literal");
	}
	const rootReal = await realpath(root);
	const matches: string[] = [];
	const served: ServedPath[] = [];
	const skipped: string[] = [];
	let outBytes = 0;
	let truncated = false;
	const state: WalkState = { visited: 0, maxTraversed: bounds.maxTraversedFiles, stop: false };
	await walk(rootReal, state, async (filePath) => {
		if (truncated || served.length >= bounds.maxMatchFiles) {
			truncated = true;
			state.stop = true;
			return;
		}
		const bytes = await readWithinBound(filePath, bounds);
		if (bytes === null || !isTextual(bytes)) {
			skipped.push(filePath);
			return;
		}
		let text: string;
		try {
			text = decodeUtf8(bytes);
		} catch (error) {
			if (error instanceof ConfinementError) {
				skipped.push(filePath);
				return;
			}
			throw error;
		}
		const lines = text.split("\n");
		let fileHits = 0;
		for (const [index, line] of lines.entries()) {
			if (!line.includes(pattern)) continue;
			const hit = `${filePath}:${index + 1}: ${line.slice(0, 400)}`;
			const hitBytes = Buffer.byteLength(hit) + 1;
			if (outBytes + hitBytes > bounds.maxToolResultBytes) {
				truncated = true;
				state.stop = true;
				if (fileHits > 0) served[served.length - 1].truncated = true;
				return;
			}
			matches.push(hit);
			outBytes += hitBytes;
			fileHits += 1;
			if (fileHits === 1) {
				served.push({
					path: filePath,
					sha256: digest(bytes),
					bytes: bytes.length,
					truncated: false,
				});
			}
			if (matches.length >= bounds.maxGrepMatches) {
				truncated = true;
				const more = lines.slice(index + 1).some((rest) => rest.includes(pattern));
				if (more) served[served.length - 1].truncated = true;
				return;
			}
		}
	});
	const content = matches.length > 0 ? matches.join("\n") : truncated
		? "bound reached before any match"
		: `no matches for ${JSON.stringify(pattern)}`;
	if (Buffer.byteLength(content) > bounds.maxToolResultBytes) {
		throw new ConfinementError(
			`grep result exceeds the ${bounds.maxToolResultBytes}-byte output bound`,
		);
	}
	return { content, details: { served_paths: served, skipped, truncated } };
}
