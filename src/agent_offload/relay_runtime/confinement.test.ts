/**
 * Contract tests for relay host-tool confinement.
 *
 * These run under `bun test` and exercise the same module the bridge loads:
 * paths outside the root (traversal, symlink escape, device, URL scheme)
 * must be refused, allowed reads must succeed with digest provenance, and
 * bounds must hold for glob and grep.
 */
import { describe, expect, test } from "bun:test";
import { chmod, mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
	assertToolArgs,
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
	maxToolArgsBytes: 64 * 1024,
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

describe("glob semantics and served-path evidence", () => {
	test("a directory-spanning glob also matches files with no intermediate directory", async () => {
		const root = await makeRoot();
		try {
			const top = await globBound(root, "**/*.txt", bounds);
			expect(top.details.served_paths.map((p) => p.path)).toEqual([join(root, "notes.txt")]);
			const nested = await globBound(root, "sub/**/*.md", bounds);
			expect(nested.details.served_paths.map((p) => p.path)).toEqual([join(root, "sub", "a.md")]);
			const exact = await globBound(root, "*.md", bounds);
			expect(exact.details.served_paths).toHaveLength(0);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});

	test("glob and grep skip an oversized file without reading it", async () => {
		const root = await makeRoot();
		const big = join(root, "big.txt");
		await writeFile(big, `needle ${"x".repeat(64)}`);
		// Unreadable, so a read before the size check fails the call.
		await chmod(big, 0o000);
		const tiny: RelayBounds = { ...bounds, maxReadBytes: 32 };
		try {
			const globbed = await globBound(root, "*.txt", tiny);
			expect(globbed.details.skipped).toEqual([big]);
			const grepped = await grepBound(root, "needle", tiny);
			expect(grepped.details.skipped).toContain(big);
			expect(grepped.details.served_paths.map((p) => p.path)).toEqual([join(root, "sub", "a.md")]);
		} finally {
			await chmod(big, 0o644);
			await rm(root, { recursive: true, force: true });
		}
	});

	test("grep marks a served file truncated when the bound cuts its matches", async () => {
		const root = await makeRoot();
		await rm(join(root, "sub"), { recursive: true });
		await writeFile(join(root, "many.txt"), "needle 1\nneedle 2\nneedle 3\n");
		try {
			const cut = await grepBound(root, "needle", { ...bounds, maxGrepMatches: 2 });
			expect(cut.details.served_paths.map((p) => p.truncated)).toEqual([true]);
			const whole = await grepBound(root, "needle", { ...bounds, maxGrepMatches: 3 });
			expect(whole.details.served_paths.map((p) => p.truncated)).toEqual([false]);
			const bytes = await grepBound(root, "needle", {
				...bounds,
				maxToolResultBytes: Buffer.byteLength(`${join(root, "many.txt")}:1: needle 1\n`) + 4,
			});
			expect(bytes.details.served_paths.map((p) => p.truncated)).toEqual([true]);
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});
});

describe("walk scope", () => {
	test("dependency and VCS directories do not spend the traversal bound", async () => {
		const root = await makeRoot();
		for (const dir of [".jj", "node_modules", ".venv"]) {
			await mkdir(join(root, dir, "deep"), { recursive: true });
			for (let i = 0; i < 20; i += 1) {
				await writeFile(join(root, dir, "deep", `f${i}.md`), "needle");
			}
		}
		const tiny: RelayBounds = { ...bounds, maxTraversedFiles: 10 };
		try {
			const globbed = await globBound(root, "**/*.md", tiny);
			expect(globbed.details.served_paths.map((p) => p.path)).toEqual([join(root, "sub", "a.md")]);
			expect(globbed.details.truncated).toBe(false);
			const grepped = await grepBound(root, "needle", tiny);
			expect(grepped.details.served_paths.map((p) => p.path)).toEqual([join(root, "sub", "a.md")]);
			// An explicit read still reaches them.
			const read = await readBound(root, "node_modules/deep/f0.md", tiny);
			expect(read.content).toBe("needle");
		} finally {
			await rm(root, { recursive: true, force: true });
		}
	});
});


async function withRoot(body: (root: string) => Promise<void>): Promise<void> {
	const root = await makeRoot();
	try {
		await body(root);
	} finally {
		await rm(root, { recursive: true, force: true });
	}
}

describe("tool argument bounds", () => {
	test("refuses unexpected keys and oversized arguments, and accepts the exact bound", () => {
		expect(() => assertToolArgs("review_read", { path: "a", extra: 1 }, ["path"], bounds)).toThrow(
			ConfinementError,
		);
		const exact = { path: "x".repeat(20 - '{"path":""}'.length) };
		const tiny: RelayBounds = { ...bounds, maxToolArgsBytes: 20 };
		expect(() => assertToolArgs("review_read", exact, ["path"], tiny)).not.toThrow();
		expect(() =>
			assertToolArgs("review_read", { path: `${exact.path}x` }, ["path"], tiny),
		).toThrow(ConfinementError);
		expect(() => assertToolArgs("review_read", null, ["path"], bounds)).not.toThrow();
	});
});

describe("read decoding and truncation", () => {
	test("refuses invalid UTF-8 and directories", async () => {
		await withRoot(async (root) => {
			await writeFile(join(root, "latin1.txt"), new Uint8Array([0x63, 0x61, 0x66, 0xe9]));
			await expect(readBound(root, "latin1.txt", bounds)).rejects.toThrow("not valid UTF-8");
			await expect(readBound(root, "sub", bounds)).rejects.toThrow("not a regular file");
		});
	});

	test("reads a file of exactly the read bound and refuses one byte more", async () => {
		await withRoot(async (root) => {
			const tiny: RelayBounds = { ...bounds, maxReadBytes: 16 };
			await writeFile(join(root, "exact.txt"), "x".repeat(16));
			await writeFile(join(root, "over.txt"), "x".repeat(17));
			expect((await readBound(root, "exact.txt", tiny)).content).toBe("x".repeat(16));
			await expect(readBound(root, "over.txt", tiny)).rejects.toThrow("read bound");
			const globbed = await globBound(root, "*.txt", tiny);
			expect(globbed.details.served_paths.map((p) => p.path)).toContain(join(root, "exact.txt"));
			expect(globbed.details.skipped).toEqual([join(root, "over.txt")]);
		});
	});

	test("truncates at the last whole line within the result bound", async () => {
		await withRoot(async (root) => {
			await writeFile(join(root, "lines.txt"), "aaaa\nbbbb\ncccc\n");
			const tiny: RelayBounds = { ...bounds, maxToolResultBytes: 12 };
			const out = await readBound(root, "lines.txt", tiny);
			expect(out.content).toBe("aaaa\nbbbb");
			expect(out.details.truncated).toBe(true);
			expect(out.details.served_paths[0].truncated).toBe(true);
			expect(out.details.served_paths[0].bytes).toBe(15);
			const whole = await readBound(root, "lines.txt", { ...bounds, maxToolResultBytes: 15 });
			expect(whole.content).toBe("aaaa\nbbbb\ncccc\n");
			expect(whole.details.truncated).toBe(false);
		});
	});

	test("truncates a line without newlines on a character boundary", async () => {
		await withRoot(async (root) => {
			// Each emoji is 4 UTF-8 bytes and two UTF-16 code units.
			await writeFile(join(root, "emoji.txt"), "ab😀😀😀");
			const out = await readBound(root, "emoji.txt", { ...bounds, maxToolResultBytes: 9 });
			expect(out.content).toBe("ab😀");
			expect(out.details.truncated).toBe(true);
			const run = await readBound(root, "emoji.txt", { ...bounds, maxToolResultBytes: 13 });
			expect(run.content).toBe("ab😀😀");
			const ascii = await readBound(root, "notes.txt", { ...bounds, maxToolResultBytes: 5 });
			expect(ascii.content).toBe("hello");
		});
	});
});

describe("walk contents and bounds", () => {
	test("a FIFO in the tree is neither read nor waited on", async () => {
		await withRoot(async (root) => {
			const made = Bun.spawnSync(["mkfifo", join(root, "pipe.txt")]);
			expect(made.exitCode).toBe(0);
			const globbed = await globBound(root, "*.txt", bounds);
			expect(globbed.details.served_paths.map((p) => p.path)).toEqual([join(root, "notes.txt")]);
			const grepped = await grepBound(root, "hello", bounds);
			expect(grepped.details.served_paths.map((p) => p.path)).toEqual([join(root, "notes.txt")]);
		});
	}, 4000);

	test("a traversal bound equal to the file count visits every file", async () => {
		await withRoot(async (root) => {
			const exact: RelayBounds = { ...bounds, maxTraversedFiles: 2 };
			const out = await globBound(root, "**/*.md", exact);
			expect(out.details.served_paths.map((p) => p.path)).toEqual([join(root, "sub", "a.md")]);
			expect(out.details.truncated).toBe(false);
		});
	});

	test("glob lists each match with its size and refuses bad patterns", async () => {
		await withRoot(async (root) => {
			const out = await globBound(root, "*.txt", bounds);
			expect(out.content).toBe(`${join(root, "notes.txt")} (11B)`);
			expect(out.details.served_paths[0].truncated).toBe(false);
			expect(out.details.truncated).toBe(false);
			expect((await globBound(root, "*.none", bounds)).content).toBe("no matching files");
			await expect(globBound(root, "../*", bounds)).rejects.toBeInstanceOf(ConfinementError);
			await expect(globBound(root, "x".repeat(513), bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
			await expect(globBound(root, "x".repeat(512), bounds)).resolves.toBeDefined();
		});
	});

	test("glob stops at the match-file and output-byte bounds", async () => {
		await withRoot(async (root) => {
			for (const name of ["one.md", "two.md", "three.md"]) {
				await writeFile(join(root, name), name);
			}
			const files = await globBound(root, "*.md", { ...bounds, maxMatchFiles: 2 });
			expect(files.details.served_paths).toHaveLength(2);
			expect(files.details.truncated).toBe(true);
			const line = `${join(root, "three.md")} (8B)`;
			const bytes = await globBound(root, "*.md", {
				...bounds,
				maxToolResultBytes: Buffer.byteLength(line) + 1,
			});
			expect(bytes.details.served_paths).toHaveLength(1);
			expect(bytes.details.truncated).toBe(true);
			expect(bytes.content.split("\n")).toHaveLength(1);
		});
	});

	test("grep reports line numbers, skips undecodable files, and refuses bad patterns", async () => {
		await withRoot(async (root) => {
			await writeFile(join(root, "two.txt"), "first\nneedle here\n");
			await writeFile(join(root, "latin1.txt"), new Uint8Array([0x6e, 0x65, 0x65, 0xe9]));
			const out = await grepBound(root, "needle here", bounds);
			expect(out.content).toBe(`${join(root, "two.txt")}:2: needle here`);
			expect(out.details.skipped).toEqual([join(root, "latin1.txt")]);
			expect((await grepBound(root, "absent", bounds)).content).toBe('no matches for "absent"');
			await expect(grepBound(root, "", bounds)).rejects.toBeInstanceOf(ConfinementError);
			await expect(grepBound(root, "x".repeat(257), bounds)).rejects.toBeInstanceOf(
				ConfinementError,
			);
			await expect(grepBound(root, "x".repeat(256), bounds)).resolves.toBeDefined();
		});
	});

	test("grep stops at the match-file bound", async () => {
		await withRoot(async (root) => {
			for (const name of ["one.txt", "two.txt", "three.txt"]) {
				await writeFile(join(root, name), "needle");
			}
			const out = await grepBound(root, "needle", { ...bounds, maxMatchFiles: 2 });
			expect(out.details.served_paths).toHaveLength(2);
			expect(out.details.truncated).toBe(true);
			expect(out.content.split("\n")).toHaveLength(2);
		});
	});

	test("a file whose single match fit is not marked truncated when the next file is cut", async () => {
		await withRoot(async (root) => {
			await rm(join(root, "sub"), { recursive: true });
			await writeFile(join(root, "a.txt"), "needle");
			await writeFile(join(root, "b.txt"), "needle");
			const hit = `${join(root, "a.txt")}:1: needle\n`;
			const out = await grepBound(root, "needle", {
				...bounds,
				maxToolResultBytes: Buffer.byteLength(hit) + 2,
			});
			expect(out.details.served_paths).toHaveLength(1);
			expect(out.details.served_paths[0].truncated).toBe(false);
			expect(out.details.truncated).toBe(true);
			const none = await grepBound(root, "needle", { ...bounds, maxToolResultBytes: 40 });
			expect(none.content).toBe("bound reached before any match");
		});
	});
});
