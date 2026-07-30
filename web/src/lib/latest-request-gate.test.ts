import { describe, expect, it } from "vitest";

import { LatestRequestGate } from "./latest-request-gate";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("LatestRequestGate", () => {
  it("rejects an older response after a newer filtered request starts", async () => {
    const gate = new LatestRequestGate();
    const first = deferred<string[]>();
    const second = deferred<string[]>();
    let results: string[] = [];
    let searching = true;

    const publish = async (request: Promise<string[]>, requestId: number) => {
      try {
        const next = await request;
        if (gate.isCurrent(requestId)) results = next;
      } finally {
        if (gate.isCurrent(requestId)) searching = false;
      }
    };

    const firstId = gate.begin();
    const firstPublish = publish(first.promise, firstId);
    const secondId = gate.begin();
    const secondPublish = publish(second.promise, secondId);

    second.resolve(["automation"]);
    await secondPublish;
    expect(results).toEqual(["automation"]);
    expect(searching).toBe(false);

    searching = true;
    first.resolve(["stale-chat"]);
    await firstPublish;
    expect(results).toEqual(["automation"]);
    expect(searching).toBe(true);
  });

  it("invalidates the active request during cleanup", () => {
    const gate = new LatestRequestGate();
    const requestId = gate.begin();

    gate.invalidate(requestId);

    expect(gate.isCurrent(requestId)).toBe(false);
  });
});
