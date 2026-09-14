import { describe, expect, it } from "vitest";

import { displayNameOfAgent } from "@/core/agents/display";

describe("displayNameOfAgent", () => {
  it("uses the product name for the built-in tender agent", () => {
    expect(displayNameOfAgent("tender-review", "tender-review")).toBe(
      "招投标智能体",
    );
  });

  it("preserves ordinary custom-agent names", () => {
    expect(displayNameOfAgent("code-reviewer", "Code Reviewer")).toBe(
      "Code Reviewer",
    );
  });
});
