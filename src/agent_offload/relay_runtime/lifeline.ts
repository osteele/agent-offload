import type { Readable } from "node:stream";

interface LifelineState {
	lost: boolean;
	watcher: (() => void) | null;
	cleanup: () => void;
}

const states = new WeakMap<Readable, LifelineState>();

/** Read one JSON line without consuming the stream to EOF. */
export function readRequestLine(stream: Readable): Promise<unknown> {
	return new Promise((resolve, reject) => {
		let line = "";
		let received = false;
		const state: LifelineState = { lost: false, watcher: null, cleanup: () => {} };
		const loss = () => {
			if (!received) {
				state.cleanup();
				reject(new Error("request ended before newline"));
				return;
			}
			if (state.lost) return;
			state.lost = true;
			state.watcher?.();
		};
		const onError = (_error: Error) => loss();
		const onData = (chunk: Buffer | string) => {
			if (received) return;
			line += chunk.toString();
			const newline = line.indexOf("\n");
			if (newline < 0) return;
			received = true;
			try {
				resolve(JSON.parse(line.slice(0, newline)) as unknown);
			} catch (error) {
				state.cleanup();
				reject(error);
			}
		};
		state.cleanup = () => {
			stream.off("data", onData);
			stream.off("end", loss);
			stream.off("error", onError);
			stream.pause();
			states.delete(stream);
		};
		states.set(stream, state);
		stream.on("data", onData);
		stream.on("end", loss);
		stream.on("error", onError);
	});
}

/** Keep the request pipe alive; return a disposer for normal completion. */
export function watchLifeline(stream: Readable, onLoss: () => void): () => void {
	const state = states.get(stream);
	if (!state) throw new Error("request line has not been read");
	state.watcher = onLoss;
	if (state.lost) onLoss();
	return state.cleanup;
}
