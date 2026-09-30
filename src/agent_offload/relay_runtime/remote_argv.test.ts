import { expect, test } from "bun:test";
import { buildRemoteArgv, REMOTE_DISABLE_FLAGS, shellQuote } from "./remote_argv.js";

test("remote argv keeps the shell alive to remove only its quoted scratch on exit", () => {
	const scratch = "/tmp/agent-offload-relay.it's here";
	const argv = buildRemoteArgv(scratch, "/opt/omp bin", ["--mode", "rpc"]);
	const quoted = shellQuote(scratch);
	expect(argv).toContain(`cd ${quoted} && trap ${shellQuote(`rm -rf -- ${quoted}`)} EXIT HUP INT TERM &&`);
	for (const flag of REMOTE_DISABLE_FLAGS) {
		expect(argv.split(" ")).toContain(shellQuote(flag));
	}
	expect(argv).not.toMatch(/\bexec\b/);
	expect(argv).toContain(shellQuote("/opt/omp bin"));
	expect(Bun.spawnSync(["sh", "-n", "-c", argv]).exitCode).toBe(0);
	const checkQuote = Bun.spawnSync(["sh", "-c", `printf %s ${quoted}`]);
	expect(checkQuote.stdout.toString()).toBe(scratch);
});
