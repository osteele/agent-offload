import { describe, expect, test } from "bun:test";
import { failedTurnMessage } from "./event_failure.js";

describe("failed OMP turn classification", () => {
	test("ignores nonterminal and successful events", () => {
		expect(failedTurnMessage({ type: "message_update" })).toBeNull();
		expect(failedTurnMessage({ type: "turn_end", stopReason: "stop" })).toBeNull();
	});

	test("surfaces a provider status string", () => {
		expect(
			failedTurnMessage({
				type: "turn_end",
				stopReason: "error",
				errorStatus: "429 rate_limit_error: retry after 78044000ms",
			}),
		).toBe("remote turn failed: 429 rate_limit_error: retry after 78044000ms");
	});

	test("surfaces the SDK turn-end message error", () => {
		expect(
			failedTurnMessage({
				type: "turn_end",
				message: {
					stopReason: "error",
					errorMessage: "429 rate_limit_error",
				},
			}),
		).toBe("remote turn failed: 429 rate_limit_error");
	});

	test("surfaces a structured provider status", () => {
		expect(
			failedTurnMessage({
				type: "turn_end",
				stopReason: "error",
				errorStatus: { status: 429, type: "rate_limit_error" },
			}),
		).toContain('"type":"rate_limit_error"');
	});
});
