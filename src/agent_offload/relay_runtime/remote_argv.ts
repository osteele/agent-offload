/** Remote surfaces disabled by both CLI flags and the scratch config overlay. */
export const REMOTE_DISABLE_FLAGS = [
	"--no-tools",
	"--no-lsp",
	"--no-extensions",
	"--no-skills",
	"--no-rules",
	"--no-session",
	"--no-pty",
	"--no-title",
] as const;

export function shellQuote(value: string): string {
	return `'${value.replaceAll("'", `'\\''`)}'`;
}

export function buildRemoteArgv(scratch: string, ompBin: string, agentArgs: string[]): string {
	const parts = [...agentArgs];
	for (const flag of REMOTE_DISABLE_FLAGS) {
		if (!parts.includes(flag)) parts.push(flag);
	}
	const cleanup = `rm -rf -- ${shellQuote(scratch)}`;
	return `cd ${shellQuote(scratch)} && trap ${shellQuote(cleanup)} EXIT HUP INT TERM && ${[ompBin, ...parts].map(shellQuote).join(" ")}`;
}
