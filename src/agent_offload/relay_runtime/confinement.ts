/**
 * Project-root confinement for locally executed relay host tools.
 *
 * Every path the remote agent names is treated as hostile: URL or device
 * schemes are rejected, parent traversal collapses against the root, and a
 * symlink anywhere on the resolved path may not leave the root. Results are
 * bounded and each served file carries a content digest so the evidence
 * stream records what the model actually saw, not what a later revision of
 * the live tree contains.
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
		joined = userPath.startsWith("/") ? await realpath(userPath) : await realpath(`${rootReal}/${userPath}`);
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

function clipToLine(bytes: Uint8Array, maxBytes: number): { text: string; truncated: boolean } {
	let view = bytes;
	let truncated = bytes.length > maxBytes;
	if (truncated) {
		view = bytes.subarray(0, maxBytes);
	}
	let text = new TextDecoder("utf-8", { fatal: false }).decode(view);
	if (truncated) {
		const lastNewline = text.lastIndexOf("\n");
		if (lastNewline > 0) text = text.slice(0, lastNewline);
	}
	return { text, truncated };
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
	const { text, truncated } = clipToLine(bytes, bounds.maxToolResultBytes);
	const served: ServedPath = {
		path: resolved,
		sha256: digest(bytes),
		bytes: bytes.length,
		truncated,
	};
	return {
		content: text,
		details: { served_paths: [served], skipped: [], truncated },
	};
}

/** List files matching a glob-like suffix under root (bounded). */
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
		const bytes = await readFile(filePath);
		if (bytes.length > bounds.maxReadBytes || !isTextual(bytes)) {
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
/** Translate a shell-style glob into a matcher over root-relative paths.
 * `**` spans directories, `*` stays within one segment. */
function globToMatcher(pattern: string): (relativePath: string) => boolean {
	const source = pattern
		.replace(/[.+^${}()|[\]\\]/g, "\\$&")
		.replace(/\*\*/g, "\u0000")
		.replace(/\*/g, "[^/]*")
		.replace(/\u0000/g, ".*")
		.replace(/\?/g, "[^/]");
	return (relativePath) => new RegExp(`^${source}$`).test(relativePath);
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
		const bytes = await readFile(filePath);
		if (bytes.length > bounds.maxReadBytes || !isTextual(bytes)) {
			skipped.push(filePath);
			return;
		}
		const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
		const lines = text.split("\n");
		let fileHits = 0;
		for (const [index, line] of lines.entries()) {
			if (!line.includes(pattern)) continue;
			const hit = `${filePath}:${index + 1}: ${line.slice(0, 400)}`;
			const hitBytes = Buffer.byteLength(hit) + 1;
			if (outBytes + hitBytes > bounds.maxToolResultBytes) {
				truncated = true;
				state.stop = true;
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
				return;
			}
		}
	});
	const content =
		matches.length > 0
			? matches.join("\n") + (truncated ? "\n...bound reached" : "")
			: truncated
				? "bound reached before any match"
				: `no matches for ${JSON.stringify(pattern)}`;
	return { content, details: { served_paths: served, skipped, truncated } };
}

interface WalkState {
	visited: number;
	maxTraversed: number;
	stop: boolean;
}

type FileVisitor = (filePath: string) => Promise<void>;

/** Bounded walk that never follows symlinks, never enters .git, and stops
 * after maxTraversed files — matching or not — so a nonmatching tree
 * terminates the call instead of funding an endless walk. */
async function walk(dir: string, state: WalkState, visit: FileVisitor): Promise<void> {
	if (state.stop) return;
	const entries = await readdir(dir, { withFileTypes: true });
	for (const entry of entries) {
		if (state.stop) return;
		if (entry.isSymbolicLink() || entry.name === ".git") continue;
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
