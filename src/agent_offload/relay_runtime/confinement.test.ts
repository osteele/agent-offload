/**
 * Contract tests for relay host-tool confinement.
 *
 * These run under `bun test` and exercise the same module the bridge loads:
 * paths outside the root (traversal, symlink escape, device, URL scheme)
 * must be refused, allowed reads must succeed with digest provenance, and
 * bounds must hold for glob and grep.
 */
import { describe, expect, test } from "bun:test";
import { mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
	ConfinementError,
	globBound,
	grepBound,
	readBound,
	type RelayBounds,
} from "./confinement.js";

const bounds: RelayBounds = {
	maxReadBytes: 512 * 1024,
	maxMatchFiles: 50,
	maxGrepMatches: 50,
	maxToolResultBytes: 256 * 1024,
	maxTraversedFiles: 2000,
};
async function makeRoot(): Promise<string> {
	const root = await realpath(await mkdtemp(join(tmpdir(), "relay-confine-")));
	await writeFile(join(root, "notes.txt"), "hello relay");
	await mkdir(join(root, "sub"));
	await writeFile(join(root, "sub", "a.md"), "needle in sub");
	return root;
}

describe("read confinement", () => {
	test("reads a file inside the root with digest provenance", async () => {
		const root = await makeRoot();
		try {
			const out = await readBound(root, "notes.txt", bounds);
			expect(out.content).toBe("hello relay");
			expect(out.details.served_paths[0].path.startsWith(root)).toBe(true);
			expect(out.details.served_paths[0].sha256).toMatch(/^[0-9a-f]{64}$/);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});

	test("refuses parent traversal", async () => {
		const root = await makeRoot();
		try {
			await expect(readBound(root, "../outside.txt", bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});

	test("refuses a symlink that escapes the root", async () => {
		const root = await makeRoot();
		const outside = join(root, "..", "outside-escape.txt");
		await writeFile(outside, "secret");
		await symlink(outside, join(root, "evil.txt"));
		try {
			await expect(readBound(root, "evil.txt", bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
		} finally {
			await rm(root, { recursive: true, force: true });
			await rm(outside, { force: true });
		}
	});

	test("refuses absolute paths outside the root and URL/device schemes", async () => {
		const root = await makeRoot();
		try {
			await expect(readBound(root, "/etc/passwd", bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
			await expect(readBound(root, "file:///etc/passwd", bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
			await expect(readBound(root, "/dev/zero", bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});

	test("refuses binary and oversized files", async () => {
		const root = await makeRoot();
		await writeFile(join(root, "bin.dat"), new Uint8Array([1, 0, 2, 0]));
		const tiny: RelayBounds = { ...bounds, maxReadBytes: 8 };
		await writeFile(join(root, "big.txt"), "x".repeat(64));
		try {
			await expect(readBound(root, "bin.dat", bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
			await expect(readBound(root, "big.txt", tiny)).rejects.toBeInstanceOf(
				ConfinementError,
			);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});
});

describe("glob and grep bounds", () => {
	test("glob lists textual files and skips binaries", async () => {
		const root = await makeRoot();
		await writeFile(join(root, "skip.dat"), new Uint8Array([0, 1]));
		try {
			const out = await globBound(root, "**/*.md", bounds);
			expect(out.details.served_paths).toHaveLength(1);
			expect(out.details.served_paths[0].path.endsWith("a.md")).toBe(true);
			expect(out.details.skipped).toHaveLength(0);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});

	test("grep finds matches and records served paths", async () => {
		const root = await makeRoot();
		try {
			const out = await grepBound(root, "needle", bounds);
			expect(out.content).toContain("a.md");
			expect(out.details.served_paths).toHaveLength(1);
			expect(out.details.served_paths[0].sha256).toMatch(/^[0-9a-f]{64}$/);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});

	test("grep bound stops collecting matches", async () => {
		const root = await makeRoot();
		await writeFile(join(root, "many.txt"), "needle\nneedle\nneedle\n");
		const tiny: RelayBounds = { ...bounds, maxGrepMatches: 2 };
		try {
			const out = await grepBound(root, "needle", tiny);
			expect(out.details.truncated).toBe(true);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});
});

describe("traversal and output byte bounds", () => {
	test("nonmatching traversal stops at the visited bound", async () => {
		const root = await makeRoot();
		const dir = join(root, "many");
		await mkdir(dir);
		for (let i = 0; i < 50; i += 1) {
			await writeFile(join(dir, `f${i}.nomatch`), "nothing here");
		}
		const tiny: RelayBounds = { ...bounds, maxTraversedFiles: 10 };
		try {
			const out = await globBound(root, "*.definitely-absent", tiny);
			expect(out.details.truncated).toBe(true);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});

	test("grep output respects the encoded byte cap", async () => {
		const root = await makeRoot();
		await writeFile(join(root, "long.txt"), `needle ${"x".repeat(300)}\n`);
		const tiny: RelayBounds = { ...bounds, maxGrepMatches: 50, maxToolResultBytes: 1024 };
		try {
			const out = await grepBound(root, "needle", tiny);
			expect(Buffer.byteLength(out.content)).toBeLessThanOrEqual(1024);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});
});
