"use client";

// Everything about one item (ticket, pull request, message, thread, channel,
// person, decision, ...) with the actions that fit it. Shown in the drawer
// on the dashboard and in the graph's side panel; links between items call
// onOpen, which each host handles its own way.

import Link from "next/link";
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Avatar } from "../avatar.tsx";
import { SourceLogo } from "../brand.tsx";
import { CheckIcon, FlagIcon, GraphIcon, SparkIcon, XIcon } from "../icons.tsx";
import { TYPE_COLOR, TYPE_ICON } from "../node-style.ts";
import { slackPermalink } from "../../lib/workspace/edits.ts";
import { NODE_TYPES } from "../../lib/workspace/graph.ts";
import type { Insight } from "../../lib/workspace/insights.ts";
import { locate } from "../../lib/workspace/locate.ts";
import { changeId, contextRefs, nodeId, personName, stageOf } from "../../lib/workspace/model.ts";
import { plainSlackText } from "../../lib/workspace/text.ts";
import { ago, stamp } from "../../lib/workspace/time.ts";
import type { SlackMessage } from "../../lib/workspace/types.ts";
import { usePreview } from "./store.tsx";
import styles from "./preview.module.css";

type Open = (id: string) => void;

export function ItemDetails({ id, onOpen, inGraph = false }: { id: string; onOpen: Open; inGraph?: boolean }) {
  const { model, nodes, insights } = usePreview();
  const node = nodes.get(id);
  const located = useMemo(() => locate(model, id), [model, id]);
  const related = useMemo(() => insights.filter((i) => i.subject === id || i.evidence.some((e) => e.nodeId === id)), [insights, id]);

  if (!node) {
    return (
      <div className={styles.details}>
        <p className="muted">This item isn&apos;t in your sources any more.</p>
      </div>
    );
  }
  const Icon = TYPE_ICON[node.type];

  return (
    <div className={styles.details}>
      <div className={styles.kindRow}>
        <span className={styles.kind} style={{ ["--c" as string]: TYPE_COLOR[node.type] }}>
          <Icon /> {NODE_TYPES[node.type].label}
        </span>
        {node.source && <SourceLogo source={node.source} size={16} />}
        {node.subgraph && <code className={styles.subgraph}>{node.subgraph}</code>}
      </div>
      <h2 className={styles.title}>{node.title}</h2>
      <p className={styles.subtitle}>{node.subtitle}</p>

      {related.map((insight) => (
        <InsightCallout key={insight.id} insight={insight} />
      ))}

      {node.body && located?.type !== "message" && located?.type !== "thread" && <p className={styles.body}>{node.body}</p>}

      {located?.type === "issue" && <IssueSection issueKey={located.issue.key} onOpen={onOpen} />}
      {located?.type === "pr" && <ChangeSection id={located.id} onOpen={onOpen} />}
      {(located?.type === "message" || located?.type === "thread") && (
        <ThreadSection message={located.type === "message" ? located.message : located.root} current={id} onOpen={onOpen} />
      )}
      {located?.type === "comment" && (
        <Section title="On ticket">
          <Row id={nodeId.issue(located.issue.key)} onOpen={onOpen} />
        </Section>
      )}
      {located?.type === "channel" && <ChannelSection channelId={located.channel.id} onOpen={onOpen} />}
      {located?.type === "repo" && (
        <Section title="Pull requests">
          {[...model.changes.values()]
            .filter((p) => p.repo === located.repo.name)
            .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
            .map((p) => (
              <Row key={changeId(p)} id={nodeId.change(changeId(p))} onOpen={onOpen} />
            ))}
        </Section>
      )}
      {located?.type === "project" && (
        <Section title="Issues">
          {[...model.issues.values()]
            .filter((i) => i.type !== "Epic")
            .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
            .map((i) => (
              <Row key={i.key} id={nodeId.issue(i.key)} onOpen={onOpen} />
            ))}
        </Section>
      )}
      {located?.type === "person" && <PersonSection personId={located.person.id} onOpen={onOpen} />}
      {located?.type === "decision" && <DecisionSection ts={located.decision.ts} onOpen={onOpen} />}
      {located?.type === "module" && (
        <Section title="Changes that touch it">
          {[...model.changes.values()]
            .filter((p) => p.repo === located.repo && p.paths.includes(located.path))
            .map((p) => (
              <Row key={changeId(p)} id={nodeId.change(changeId(p))} onOpen={onOpen} />
            ))}
        </Section>
      )}

      {node.fields.length > 0 && (
        <dl className={styles.fields}>
          {node.fields.map((f) => (
            <div key={f.label}>
              <dt>{f.label}</dt>
              <dd>{f.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <Connections id={id} onOpen={onOpen} />

      <div className={styles.footer}>
        {!inGraph && (
          <Link className="btn btn-secondary btn-sm" href={`/graph?focus=${encodeURIComponent(id)}`}>
            <GraphIcon /> Show in graph
          </Link>
        )}
        <span className="subtle">Preview data: changes stay in this browser.</span>
      </div>
    </div>
  );
}

// --- pieces ------------------------------------------------------------------

function Section({ title, count, children, action }: { title: string; count?: number; children: ReactNode; action?: ReactNode }) {
  return (
    <section className={styles.section}>
      <div className={styles.sectionHead}>
        <h3>
          {title} {count !== undefined && <span className="subtle">{count}</span>}
        </h3>
        {action}
      </div>
      <div className={styles.sectionBody}>{children}</div>
    </section>
  );
}

/** A clickable line for any item, styled by its node type. */
export function Row({ id, onOpen, secondary, meta }: { id: string; onOpen: Open; secondary?: string; meta?: string }) {
  const { nodes } = usePreview();
  const node = nodes.get(id);
  if (!node) return null;
  return (
    <button type="button" className={styles.row} onClick={() => onOpen(id)}>
      <span className={styles.dot} style={{ background: TYPE_COLOR[node.type] }} />
      <span className={styles.rowText}>
        <span className={styles.rowPrimary}>{node.type === "Message" ? node.body ?? node.title : node.title}</span>
        <span className={styles.rowSecondary}>{secondary ?? node.subtitle}</span>
      </span>
      {meta && <span className={styles.rowMeta}>{meta}</span>}
    </button>
  );
}

function InsightCallout({ insight }: { insight: Insight }) {
  const { apply, dismiss } = usePreview();
  return (
    <div className={styles.callout} data-severity={insight.severity}>
      <p className={styles.calloutTitle}>{insight.title}</p>
      <p className={styles.calloutText}>{insight.summary}</p>
      <div className={styles.calloutActions}>
        {insight.actions.map((a, n) => (
          <button key={a.label} type="button" className={`btn btn-sm ${n === 0 ? "btn-primary" : "btn-secondary"}`} onClick={() => apply(a.edit, a.done)}>
            {n === 0 && <SparkIcon />}
            {a.label}
          </button>
        ))}
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => dismiss(insight)}>
          Dismiss
        </button>
      </div>
    </div>
  );
}

function Composer({ placeholder, button, onSend }: { placeholder: string; button: string; onSend: (text: string) => void }) {
  const [text, setText] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    onSend(text.trim());
    setText("");
  };
  return (
    <form className={styles.composer} onSubmit={submit}>
      <textarea
        className="input"
        rows={2}
        value={text}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e);
        }}
      />
      <button type="submit" className="btn btn-secondary btn-sm" disabled={!text.trim()}>
        {button}
      </button>
    </form>
  );
}

function MessageBubble({ message, current, onOpen }: { message: SlackMessage; current?: boolean; onOpen: Open }) {
  const { model, now } = usePreview();
  const name = personName(model, "slack", message.user);
  const text = plainSlackText(message.text, (uid) => model.byAccount.slack.get(uid)?.name);
  return (
    <button type="button" className={styles.bubble} data-current={current} onClick={() => onOpen(nodeId.message(message.ts))}>
      <Avatar name={name} size="sm" />
      <span className={styles.bubbleBody}>
        <span className={styles.bubbleHead}>
          <b>{name}</b>
          <time className="subtle" title={stamp(message.at)}>
            {ago(message.at, now)}
          </time>
        </span>
        <span className={styles.bubbleText}>{text}</span>
        {message.reactions && (
          <span className={styles.reactions}>
            {message.reactions.map((r) => (
              <span key={r.name}>
                :{r.name}: {r.count}
              </span>
            ))}
          </span>
        )}
      </span>
    </button>
  );
}

// --- per type ------------------------------------------------------------------

function workflow(model: ReturnType<typeof usePreview>["model"]): string[] {
  const names = new Set<string>();
  for (const i of model.issues.values()) {
    names.add(i.status);
    for (const h of i.history) {
      names.add(h.from);
      names.add(h.to);
    }
  }
  const order = { todo: 0, in_progress: 1, in_review: 2, done: 3 };
  return [...names].sort((a, b) => order[stageOf(a)] - order[stageOf(b)] || a.localeCompare(b));
}

function IssueSection({ issueKey, onOpen }: { issueKey: string; onOpen: Open }) {
  const { model, apply, now } = usePreview();
  const issue = model.issues.get(issueKey)!;
  const statuses = useMemo(() => workflow(model), [model]);
  const prs = model.issueChanges.get(issue.key) ?? [];
  const children = [...model.issues.values()].filter((i) => i.epic === issue.key);
  const mentions = [...model.messages.values()]
    .filter((m) => contextRefs(model, m).issues.includes(issue.key))
    .sort((a, b) => b.at.localeCompare(a.at));

  return (
    <>
      <div className={styles.controls}>
        <label className={styles.control}>
          <span>Status</span>
          <select
            className="input"
            value={issue.status}
            onChange={(e) => apply({ kind: "issue_status", key: issue.key, to: e.target.value }, `Moved ${issue.key} to ${e.target.value}`)}
          >
            {statuses.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className={`btn btn-sm ${issue.flagged ? "btn-primary" : "btn-secondary"}`}
          aria-pressed={!!issue.flagged}
          onClick={() =>
            apply(
              { kind: "issue_flag", key: issue.key, flagged: !issue.flagged },
              issue.flagged ? `Removed the flag from ${issue.key}` : `Flagged ${issue.key} as blocked`,
            )
          }
        >
          <FlagIcon /> {issue.flagged ? "Flagged as blocked" : "Flag as blocked"}
        </button>
      </div>

      {children.length > 0 && (
        <Section title="Issues in this epic" count={children.length}>
          {children.map((c) => (
            <Row key={c.key} id={nodeId.issue(c.key)} onOpen={onOpen} />
          ))}
        </Section>
      )}

      <Section title="Pull requests" count={prs.length}>
        {prs.length ? prs.map((p) => <Row key={p} id={nodeId.change(p)} onOpen={onOpen} />) : <p className={styles.empty}>No pull request names {issue.key}.</p>}
      </Section>

      {mentions.length > 0 && (
        <Section title="Discussed in Slack" count={mentions.length}>
          {mentions.slice(0, 6).map((m) => (
            <MessageBubble key={m.ts} message={m} onOpen={onOpen} />
          ))}
        </Section>
      )}

      <Section title="Comments" count={issue.comments?.length ?? 0}>
        {(issue.comments ?? []).map((c) => (
          <div key={c.id} className={styles.comment}>
            <Avatar name={personName(model, "jira", c.author)} size="sm" />
            <div>
              <p className={styles.bubbleHead}>
                <b>{personName(model, "jira", c.author)}</b>
                <time className="subtle" title={stamp(c.at)}>
                  {ago(c.at, now)}
                </time>
              </p>
              <p className={styles.bubbleText}>{c.body}</p>
            </div>
          </div>
        ))}
        <Composer placeholder={`Comment on ${issue.key}…`} button="Comment" onSend={(body) => apply({ kind: "issue_comment", key: issue.key, body }, `Commented on ${issue.key}`)} />
      </Section>
    </>
  );
}

function ChangeSection({ id, onOpen }: { id: string; onOpen: Open }) {
  const { model, apply, now } = usePreview();
  const pr = model.changes.get(id)!;
  const tickets = model.changeIssues.get(id) ?? [];
  const [linkKey, setLinkKey] = useState("");
  const candidates = [...model.issues.values()].filter((i) => i.type !== "Epic" && stageOf(i.status) !== "done");
  const mentions = [...model.messages.values()]
    .filter((m) => model.messageRefs.get(m.ts)!.changes.includes(id))
    .sort((a, b) => b.at.localeCompare(a.at));
  const channel = mentions[0]?.channel ?? model.ws.slack?.channels[0]?.id;
  const open = pr.state === "open" || pr.state === "draft";

  return (
    <>
      <div className={styles.controls}>
        <span className={`badge ${pr.state === "merged" ? "badge-merged" : pr.state === "draft" ? "" : "badge-success"}`}>
          {pr.state === "merged" ? `Merged ${ago(pr.mergedAt!, now)}` : pr.state === "draft" ? "Draft" : pr.state === "closed" ? "Closed" : "Open"}
        </span>
        <span className={`badge ${pr.checks === "failing" ? "badge-danger" : pr.checks === "passing" ? "badge-success" : ""}`}>
          {pr.checks === "failing" ? <XIcon /> : pr.checks === "passing" ? <CheckIcon /> : null}
          Checks {pr.checks}
        </span>
        {open && pr.checks === "failing" && (
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => apply({ kind: "pr_checks", change: id, checks: "passing" }, `Checks pass on ${id} now`)}>
            Re-run checks
          </button>
        )}
      </div>

      <Section title="Reviews" count={pr.reviews.length}>
        {pr.reviews.length === 0 && <p className={styles.empty}>Nobody is reviewing yet.</p>}
        {pr.reviews.map((r) => {
          const person = model.byAccount.github.get(r.reviewer);
          const slackId = person?.accounts.slack;
          const label = { requested: "Review requested", approved: "Approved", changes_requested: "Changes requested", commented: "Commented" }[r.state];
          return (
            <div key={r.reviewer} className={styles.review}>
              <Avatar name={person?.name ?? r.reviewer} size="sm" />
              <span className={styles.rowText}>
                <span className={styles.rowPrimary}>{person?.name ?? r.reviewer}</span>
                <span className={styles.rowSecondary}>
                  {label}
                  {r.at ? ` · ${ago(r.at, now)}` : ""}
                </span>
              </span>
              {r.state === "requested" && open && slackId && channel && model.byAccount.slack.has(slackId) && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() =>
                    apply(
                      { kind: "slack_message", channel, text: `<@${slackId}> could you review ${id} when you get a chance?` },
                      `Asked ${person!.name} to review ${id}`,
                    )
                  }
                >
                  Nudge
                </button>
              )}
            </div>
          );
        })}
      </Section>

      <Section title="Tickets" count={tickets.length}>
        {tickets.map((k) => (
          <Row key={k} id={nodeId.issue(k)} onOpen={onOpen} />
        ))}
        {tickets.length === 0 && model.ws.jira && (
          <form
            className={styles.inline}
            onSubmit={(e) => {
              e.preventDefault();
              if (!linkKey) return;
              apply({ kind: "pr_title", change: id, title: `${linkKey}: ${pr.title}` }, `Linked ${id} to ${linkKey}`);
              setLinkKey("");
            }}
          >
            <select className="input" value={linkKey} onChange={(e) => setLinkKey(e.target.value)} aria-label="Ticket to link">
              <option value="">Link a ticket…</option>
              {candidates.map((i) => (
                <option key={i.key} value={i.key}>
                  {i.key}: {i.summary}
                </option>
              ))}
            </select>
            <button type="submit" className="btn btn-secondary btn-sm" disabled={!linkKey}>
              Link
            </button>
          </form>
        )}
      </Section>

      {mentions.length > 0 && (
        <Section title="Mentioned in Slack" count={mentions.length}>
          {mentions.map((m) => (
            <MessageBubble key={m.ts} message={m} onOpen={onOpen} />
          ))}
        </Section>
      )}
    </>
  );
}

function ThreadSection({ message, current, onOpen }: { message: SlackMessage; current: string; onOpen: Open }) {
  const { model, apply } = usePreview();
  const root = message.threadTs ? model.messages.get(message.threadTs) ?? message : message;
  const replies = model.replies.get(root.ts) ?? [];
  const refs = contextRefs(model, message);
  const channel = model.ws.slack?.channels.find((c) => c.id === root.channel);

  return (
    <>
      <Section title={replies.length ? `Thread in #${channel?.name}` : `In #${channel?.name}`} count={replies.length ? replies.length + 1 : undefined}>
        <MessageBubble message={root} current={nodeId.message(root.ts) === current || nodeId.thread(root.ts) === current} onOpen={onOpen} />
        {replies.map((r) => (
          <div key={r.ts} className={styles.reply}>
            <MessageBubble message={r} current={nodeId.message(r.ts) === current} onOpen={onOpen} />
          </div>
        ))}
        <Composer
          placeholder="Reply in thread…"
          button="Reply"
          onSend={(text) => apply({ kind: "slack_message", channel: root.channel, text, threadTs: root.ts }, `Replied in #${channel?.name}`)}
        />
      </Section>
      {(refs.issues.length > 0 || refs.changes.length > 0) && (
        <Section title="About">
          {refs.issues.map((k) => (
            <Row key={k} id={nodeId.issue(k)} onOpen={onOpen} />
          ))}
          {refs.changes.map((c) => (
            <Row key={c} id={nodeId.change(c)} onOpen={onOpen} />
          ))}
        </Section>
      )}
    </>
  );
}

function ChannelSection({ channelId, onOpen }: { channelId: string; onOpen: Open }) {
  const { model, apply } = usePreview();
  const channel = model.ws.slack!.channels.find((c) => c.id === channelId)!;
  const recent = [...model.messages.values()]
    .filter((m) => m.channel === channelId && !m.threadTs)
    .sort((a, b) => b.at.localeCompare(a.at))
    .slice(0, 10);
  return (
    <Section title="Latest messages" count={recent.length}>
      <Composer placeholder={`Message #${channel.name}…`} button="Send" onSend={(text) => apply({ kind: "slack_message", channel: channelId, text }, `Posted in #${channel.name}`)} />
      {recent.map((m) => (
        <MessageBubble key={m.ts} message={m} onOpen={onOpen} />
      ))}
    </Section>
  );
}

function PersonSection({ personId, onOpen }: { personId: string; onOpen: Open }) {
  const { model } = usePreview();
  const person = model.people.get(personId)!;
  const { github, jira, slack } = person.accounts;
  const assigned = [...model.issues.values()].filter((i) => jira && i.assignee === jira && stageOf(i.status) !== "done");
  const prs = [...model.changes.values()].filter((p) => github && p.author === github);
  const messages = [...model.messages.values()].filter((m) => slack && m.user === slack).sort((a, b) => b.at.localeCompare(a.at));
  return (
    <>
      <Section title="Accounts">
        <div className={styles.accounts}>
          {github && (
            <span>
              <SourceLogo source="github" size={14} /> {github}
            </span>
          )}
          {jira && (
            <span>
              <SourceLogo source="jira" size={14} /> {jira}
            </span>
          )}
          {slack && (
            <span>
              <SourceLogo source="slack" size={14} /> {slack}
            </span>
          )}
        </div>
      </Section>
      {assigned.length > 0 && (
        <Section title="Working on" count={assigned.length}>
          {assigned.map((i) => (
            <Row key={i.key} id={nodeId.issue(i.key)} onOpen={onOpen} />
          ))}
        </Section>
      )}
      {prs.length > 0 && (
        <Section title="Pull requests" count={prs.length}>
          {prs.map((p) => (
            <Row key={changeId(p)} id={nodeId.change(changeId(p))} onOpen={onOpen} />
          ))}
        </Section>
      )}
      {messages.length > 0 && (
        <Section title="Recent messages" count={messages.length}>
          {messages.slice(0, 5).map((m) => (
            <MessageBubble key={m.ts} message={m} onOpen={onOpen} />
          ))}
        </Section>
      )}
    </>
  );
}

function DecisionSection({ ts, onOpen }: { ts: string; onOpen: Open }) {
  const { model, apply, workspace } = usePreview();
  const d = model.decisions.find((x) => x.ts === ts)!;
  const m = model.messages.get(ts)!;
  const channel = model.ws.slack?.channels.find((c) => c.id === m.channel)?.name ?? m.channel;
  const unrecorded = d.issues.filter((k) => model.issues.has(k) && !d.recordedIn.some((r) => r.key === k));
  return (
    <>
      <Section title="Decided in">
        <MessageBubble message={m} onOpen={onOpen} />
      </Section>
      <Section title="Recorded on" count={d.recordedIn.length}>
        {d.recordedIn.map((r) => (
          <Row key={r.commentId} id={nodeId.issue(r.key)} onOpen={onOpen} secondary="A comment links to this decision" />
        ))}
        {unrecorded.map((k) => (
          <button
            key={k}
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() =>
              apply(
                { kind: "issue_comment", key: k, body: `Decision from #${channel}: ${d.statement} ${slackPermalink(workspace, m.channel, m.ts)}` },
                `Added the decision to ${k}`,
              )
            }
          >
            Record it on {k}
          </button>
        ))}
        {d.recordedIn.length === 0 && unrecorded.length === 0 && <p className={styles.empty}>Not tied to a ticket.</p>}
      </Section>
    </>
  );
}

function Connections({ id, onOpen }: { id: string; onOpen: Open }) {
  const { adjacency, nodes } = usePreview();
  const [open, setOpen] = useState(false);
  const list = adjacency.get(id) ?? [];
  if (list.length === 0) return null;
  const groups = new Map<string, typeof list>();
  for (const c of list) {
    const k = c.out ? `${c.edge.type} →` : `← ${c.edge.type}`;
    groups.set(k, [...(groups.get(k) ?? []), c]);
  }
  return (
    <section className={styles.section}>
      <button type="button" className={styles.disclosure} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span>All connections</span> <span className="subtle">{list.length}</span>
        <span className={styles.chev} aria-hidden="true">
          {open ? "−" : "+"}
        </span>
      </button>
      {open &&
        [...groups.entries()].map(([k, items]) => (
          <div key={k} className={styles.connGroup}>
            <p className={styles.connType}>{k}</p>
            {items.map((c) => (
              <Row key={c.edge.id} id={c.other} onOpen={onOpen} meta={c.edge.detail} secondary={nodes.get(c.other)?.subtitle} />
            ))}
          </div>
        ))}
    </section>
  );
}

/** A titled list of items, for "Open pull requests" and the like. */
export function ItemList({ ids, onOpen, empty }: { ids: string[]; onOpen: Open; empty?: string }) {
  if (ids.length === 0) return <p className={styles.empty}>{empty ?? "Nothing here."}</p>;
  return (
    <div className={styles.sectionBody}>
      {ids.map((id) => (
        <Row key={id} id={id} onOpen={onOpen} />
      ))}
    </div>
  );
}
