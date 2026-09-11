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
	const skipped: string[] = [];
	await walk(rootReal, rootReal, async (filePath) => {
		if (matched.length >= bounds.maxMatchFiles) return;
		const relative = filePath.slice(rootReal.length + 1);
		if (!glob(relative)) return;
		const bytes = await readFile(filePath);
		if (bytes.length > bounds.maxReadBytes || !isTextual(bytes)) {
			skipped.push(filePath);
			return;
		}
		matched.push({
			path: filePath,
			sha256: digest(bytes),
			bytes: bytes.length,
			truncated: false,
		});
	});
	if (matched.length >= bounds.maxMatchFiles) {
		const truncatedOutput = `file bound ${bounds.maxMatchFiles} reached; listing first files:\n${matched
			.map((entry) => `${entry.path} (${entry.bytes}B)`)
			.join("\n")}`;
		return {
			content: truncatedOutput,
			details: { served_paths: matched, skipped, truncated: true },
		};
	}
	const listing = matched.length
		? matched.map((entry) => `${entry.path} (${entry.bytes}B)`).join("\n")
		: "no matching files";
	return { content: listing, details: { served_paths: matched, skipped, truncated: false } };
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
	let bytesSeen = 0;
	let truncated = false;
	await walk(rootReal, rootReal, async (filePath) => {
		if (truncated || served.length >= bounds.maxMatchFiles) {
			truncated = true;
			return;
		}
		const bytes = await readFile(filePath);
		if (bytes.length > bounds.maxReadBytes || !isTextual(bytes)) {
			skipped.push(filePath);
			return;
		}
		const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
		const lines = text.split("\n");
		const hits: string[] = [];
		for (const [index, line] of lines.entries()) {
			if (line.includes(pattern)) {
				hits.push(`${filePath}:${index + 1}: ${line.slice(0, 400)}`);
				if (hits.length >= bounds.maxGrepMatches) break;
			}
		}
		if (hits.length > 0) {
			matches.push(...hits);
			served.push({
				path: filePath,
				sha256: digest(bytes),
				bytes: bytes.length,
				truncated: false,
			});
			if (matches.length >= bounds.maxGrepMatches) {
				truncated = true;
			}
		}
		bytesSeen += bytes.length;
		if (bytesSeen > bounds.maxToolResultBytes * 16) {
			truncated = true;
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

type FileVisitor = (filePath: string) => Promise<void>;

/** Bounded walk that never follows symlinks and never enters .git. */
async function walk(rootReal: string, dir: string, visit: FileVisitor): Promise<void> {
	const entries = await readdir(dir, { withFileTypes: true });
	for (const entry of entries) {
		if (entry.isSymbolicLink() || entry.name === ".git") continue;
		const child = `${dir}/${entry.name}`;
		if (entry.isDirectory()) {
			await walk(rootReal, child, visit);
		} else if (entry.isFile()) {
			await visit(child);
		}
	}
}
