import { describe, expect, it } from "vitest";
import { statusLabel } from "./components/Badges";
import { fmtDuration, pct } from "./core";

describe("formatting", () => {
  it("formats durations", () => {
    expect(fmtDuration(42)).toBe("42s");
    expect(fmtDuration(125)).toBe("2m 5s");
    expect(fmtDuration(3720)).toBe("1h 2m");
    expect(fmtDuration(null)).toBe("—");
  });
  it("formats ratios with precision for small values", () => {
    expect(pct(0.054)).toBe("5.4%");
    expect(pct(0.5)).toBe("50%");
  });
  it("labels every incident status", () => {
    expect(statusLabel("AWAITING_APPROVAL")).toBe("Awaiting approval");
    expect(statusLabel("NEEDS_HUMAN")).toBe("Needs human");
  });
});
