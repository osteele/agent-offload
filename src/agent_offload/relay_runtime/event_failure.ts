const MAX_DETAIL_BYTES = 2048;

function clipDetail(detail: string): string {
	const bytes = Buffer.from(detail);
	if (bytes.length <= MAX_DETAIL_BYTES) return detail;
	return bytes.subarray(0, MAX_DETAIL_BYTES).toString("utf8");
}

/** Return a bounded provider diagnostic for a failed OMP turn. */
export function failedTurnMessage(event: unknown): string | null {
	if (!event || typeof event !== "object") return null;
	const record = event as Record<string, unknown>;
	if (record.type !== "turn_end") return null;
	const message =
		record.message && typeof record.message === "object"
			? (record.message as Record<string, unknown>)
			: null;
	const stopReason = record.stopReason ?? message?.stopReason;
	if (stopReason !== "error") return null;

	const status =
		record.errorStatus ??
		record.errorMessage ??
		message?.errorStatus ??
		message?.errorMessage;
	let detail = "";
	if (typeof status === "string") {
		detail = status;
	} else if (status !== undefined && status !== null) {
		try {
			detail = JSON.stringify(status);
		} catch {
			detail = String(status);
		}
	}
	return detail
		? `remote turn failed: ${clipDetail(detail)}`
		: "remote turn failed with stopReason=error";
}
