import { ApiPreview } from "./api-preview";

export default function HomePage() {
  return (
    <main>
      <h1>Haumea + Next.js</h1>
      <p>
        Next.js serves <code>/</code> while a Haumea service in a uv workspace
        handles <code>/api</code>.
      </p>
      <ul>
        <li>
          <a href="/api">/api</a>
        </li>
        <li>
          <a href="/api/hello?name=world">/api/hello?name=world</a>
        </li>
        <li>
          <a href="/api/items/42">/api/items/42</a>
        </li>
        <li>
          <a href="/api/docs">/api/docs</a>
        </li>
        <li>
          <a href="/api/openapi.json">/api/openapi.json</a>
        </li>
        <li>
          <a href="/api/hello.txt">/api/hello.txt</a>
        </li>
      </ul>
      <ApiPreview />
    </main>
  );
}
