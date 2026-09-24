import {
  createRootRoute,
  HeadContent,
  Outlet,
  Scripts,
} from "@tanstack/react-router";
import styles from "../styles.css?url";

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "EAS evals" },
    ],
    links: [{ rel: "stylesheet", href: styles }],
  }),
  component: () => (
    <html lang="en">
      <head>
        <HeadContent />
      </head>
      <body>
        <Outlet />
        <Scripts />
      </body>
    </html>
  ),
  errorComponent: ({ error }) => (
    <main className="fatal">
      <h1>Couldn’t open your evals</h1>
      <p>
        {error instanceof Error
          ? error.message
          : "The server could not load this page."}
      </p>
      <button onClick={() => window.location.reload()}>Try again</button>
    </main>
  ),
});
