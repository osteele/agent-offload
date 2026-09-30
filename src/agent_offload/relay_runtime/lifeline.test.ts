import { expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import { readRequestLine, watchLifeline } from "./lifeline.js";

test("parses only the first newline-terminated request; later data is not loss", async () => {
	const pipe = new PassThrough();
	const request = readRequestLine(pipe);
	pipe.write('{"prompt":"first"}\nextra data\n');
	expect(await request).toEqual({ prompt: "first" });
	let losses = 0;
	const dispose = watchLifeline(pipe, () => { losses++; });
	pipe.write("more data");
	expect(losses).toBe(0);
	pipe.end();
	await new Promise<void>((resolve) => pipe.once("end", resolve));
	expect(losses).toBe(1);
	dispose();
});

test("EOF after request calls loss only once", async () => {
	const pipe = new PassThrough();
	const request = readRequestLine(pipe);
	pipe.write("{}\n");
	await request;
	let losses = 0;
	const dispose = watchLifeline(pipe, () => { losses++; });
	pipe.end();
	await new Promise<void>((resolve) => pipe.once("end", resolve));
	pipe.emit("error", new Error("late error"));
	expect(losses).toBe(1);
	dispose();
});

test("EOF before newline rejects the request", async () => {
	const pipe = new PassThrough();
	const request = readRequestLine(pipe);
	pipe.end('{"prompt":"incomplete"}');
	await expect(request).rejects.toThrow("before newline");
});
