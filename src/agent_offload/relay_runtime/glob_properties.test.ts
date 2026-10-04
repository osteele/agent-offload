/**
 * Generated patterns and paths: the relay's glob matcher must agree with
 * Bun.Glob, an independent implementation, on the grammar the relay
 * supports (literal segments, `*`, `?`, and `**`). Dotfiles and bracket
 * classes are left out because the relay deliberately treats them
 * differently. Replay with RELAY_GLOB_SEED=<seed>.
 */
import { expect, test } from "bun:test";
import { globToMatcher } from "./confinement.js";

function generator(seed: number) {
	let state = seed >>> 0 || 1;
	const next = () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return (state >>> 0) / 4294967296;
	};
	const pick = <T,>(items: readonly T[]): T => items[Math.floor(next() * items.length)] as T;
	return { next, pick };
}

const LETTERS = ["a", "b", "ab", "a.ts", "b.md", "x-y"] as const;
const PATTERN_SEGMENTS = [
	"*", "?", "a*", "*.ts", "*.md", "a?", "**", "a**", "**b", "a**b", "**.ts", "*a*", ...LETTERS,
] as const;

function sample(seed: number) {
	const { next, pick } = generator(seed);
	const depth = 1 + Math.floor(next() * 4);
	const path = Array.from({ length: depth }, () => pick(LETTERS)).join("/");
	const patternDepth = 1 + Math.floor(next() * 4);
	const pattern = Array.from({ length: patternDepth }, () => pick(PATTERN_SEGMENTS)).join("/");
	return { path, pattern };
}

const seeds = process.env.RELAY_GLOB_SEED
	? [Number(process.env.RELAY_GLOB_SEED)]
	: Array.from({ length: 20000 }, (_, i) => i + 1);

test("relay glob agrees with Bun.Glob on generated patterns", () => {
	const features = {
		doubleStar: 0, embeddedDoubleStar: 0, leadingDoubleStar: 0, matched: 0, skipped: 0,
	};
	for (const seed of seeds) {
		const { path, pattern } = sample(seed);
		if (pattern.includes("**")) features.doubleStar += 1;
		if (/[^/]\*\*|\*\*[^/]/.test(pattern)) features.embeddedDoubleStar += 1;
		if (pattern.startsWith("**/")) features.leadingDoubleStar += 1;
		// Bun.Glob lets `a**/**` match `a` although `a*/**` does not; that
		// shape has no consistent reference answer, so it is not compared.
		if (/(^|\/)[^/]*[^/]\*\*[^/]*\/\*\*(\/|$)|(^|\/)\*\*[^/]+\/\*\*(\/|$)/.test(pattern)) {
			features.skipped += 1;
			continue;
		}
		const expected = new Bun.Glob(pattern).match(path);
		if (expected) features.matched += 1;
		const actual = globToMatcher(pattern)(path);
		if (actual !== expected) {
			throw new Error(
				`seed ${seed}: pattern ${JSON.stringify(pattern)} path ${JSON.stringify(path)} ` +
					`relay=${actual} Bun.Glob=${expected}; replay with RELAY_GLOB_SEED=${seed}`,
			);
		}
	}
	if (seeds.length > 1) {
		// The generator must actually reach the cases that matter.
		expect(features.doubleStar).toBeGreaterThan(1000);
		expect(features.leadingDoubleStar).toBeGreaterThan(100);
		expect(features.embeddedDoubleStar).toBeGreaterThan(1000);
		expect(features.skipped).toBeLessThan(seeds.length / 20);
		expect(features.matched).toBeGreaterThan(500);
	}
});
