"use client";

// "Works in your agent": one tab per agent. Each shows the agent calling the
// MCP server's `search` tool (apps/mcp, EMBER-79 prototype) and the hits it
// gets back from the preview data, plus the command that adds the server.

import { useState, type KeyboardEvent } from "react";
import { ClaudeLogo, CodexLogo, CursorLogo } from "../logos/index.ts";
import { SourceLogo } from "../brand.tsx";
import type { SearchHit } from "./data.ts";
import styles from "./agents.module.css";

const DB = "postgresql://…";

const AGENTS = [
  {
    id: "claude",
    name: "Claude Code",
    Logo: ClaudeLogo,
    prompt: ">",
    call: "ember - search (MCP)",
    config: `claude mcp add ember \\
  -e DATABASE_URL='${DB}' \\
  -e PYTHONPATH="$PWD/apps/mcp" \\
  -- "$PWD/.venv/bin/python" -m ember_mcp`,
  },
  {
    id: "codex",
    name: "Codex",
    Logo: CodexLogo,
    prompt: "›",
    call: "Called ember.search",
    config: `codex mcp add ember \\
  --env DATABASE_URL='${DB}' \\
  --env PYTHONPATH="$PWD/apps/mcp" \\
  -- "$PWD/.venv/bin/python" -m ember_mcp`,
  },
  {
    id: "cursor",
    name: "Cursor",
    Logo: CursorLogo,
    prompt: "",
    call: "Ran MCP tool search",
    config: `// .cursor/mcp.json
{
  "mcpServers": {
    "ember": {
      "command": ".venv/bin/python",
      "args": ["-m", "ember_mcp"],
      "env": { "DATABASE_URL": "${DB}", "PYTHONPATH": "apps/mcp" }
    }
  }
}`,
  },
] as const;

/** Renders ts_headline-style **bold** matches. */
function Snippet({ text }: { text: string }) {
  return (
    <>
      {text.split(/\*\*(.+?)\*\*/g).map((part, i) => (i % 2 ? <b key={i}>{part}</b> : part))}
    </>
  );
}

export function AgentTabs({ question, hits }: { question: string; hits: SearchHit[] }) {
  const [active, setActive] = useState(0);
  const agent = AGENTS[active];

  const onKey = (e: KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = (active + (e.key === "ArrowRight" ? 1 : AGENTS.length - 1)) % AGENTS.length;
    setActive(next);
    document.getElementById(`agent-tab-${AGENTS[next].id}`)?.focus();
  };

  return (
    <div className={styles.agents}>
      <div className={styles.tabs} role="tablist" aria-label="Agent" onKeyDown={onKey}>
        {AGENTS.map((a, i) => (
          <button
            key={a.id}
            id={`agent-tab-${a.id}`}
            type="button"
            role="tab"
            aria-selected={i === active}
            aria-controls="agent-panel"
            tabIndex={i === active ? 0 : -1}
            className={styles.tab}
            onClick={() => setActive(i)}
          >
            <a.Logo size={14} />
            {a.name}
          </button>
        ))}
      </div>
      <div id="agent-panel" role="tabpanel" aria-labelledby={`agent-tab-${agent.id}`} className={styles.panel} data-agent={agent.id} key={agent.id}>
        <div className={styles.session}>
          <p className={styles.prompt}>
            {agent.prompt && <span className={styles.caret}>{agent.prompt}</span>}
            What do we have on saved cards?
          </p>
          <p className={styles.call}>
            <span className={styles.callDot} />
            <code>
              {agent.call}
              <span className={styles.args}>
                (question: &quot;{question}&quot;, k: {hits.length})
              </span>
            </code>
          </p>
          <ol className={styles.hits}>
            {hits.map((h) => (
              <li key={h.object_id}>
                <SourceLogo source={h.source} size={14} />
                <span className={styles.hitBody}>
                  <span className={styles.hitHead}>
                    <code>{h.kind}</code>
                    <code className={styles.hitId}>{h.object_id}</code>
                  </span>
                  <span className={styles.hitSnippet}>
                    <Snippet text={h.snippet} />
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </div>
        <pre className={styles.config}>
          <code>{agent.config}</code>
        </pre>
      </div>
    </div>
  );
}
