"use client";

import { useEffect, useState } from "react";

export function ApiPreview() {
  const [payload, setPayload] = useState<string>("Loading /api...");

  useEffect(() => {
    fetch("/api")
      .then((res) => res.json())
      .then((data) => setPayload(JSON.stringify(data, null, 2)))
      .catch((err: unknown) => setPayload(`Error: ${String(err)}`));
  }, []);

  return (
    <section>
      <h2>Response from /api</h2>
      <pre>{payload}</pre>
    </section>
  );
}
