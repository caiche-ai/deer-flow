import { afterEach, expect, test, vi } from "vitest";

import { uploadFiles } from "@/core/uploads/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("uploadFiles sends the custom agent name as a query parameter", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ success: true, files: [], message: "ok" }),
  });
  vi.stubGlobal("fetch", fetchMock);

  await uploadFiles("thread-1", [new File(["pdf"], "招标文件.pdf")], {
    agentName: "tender-review",
  });

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/threads/thread-1/uploads?agent_name=tender-review",
    expect.objectContaining({ method: "POST" }),
  );
});

test("uploadFiles preserves a plain-text proxy error", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response("Gateway timed out while parsing PDF", {
        status: 504,
        statusText: "Gateway Timeout",
      }),
    ),
  );

  await expect(
    uploadFiles("thread-1", [new File(["pdf"], "a.pdf")]),
  ).rejects.toThrow("Gateway timed out while parsing PDF");
});
