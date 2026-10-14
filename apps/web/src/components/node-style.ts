// How each ontology node type looks, everywhere it's shown: the graph, the
// detail views, and lists of items.

import type { NodeType } from "../lib/workspace/graph.ts";
import {
  ContainerIcon,
  DecisionIcon,
  IdentityIcon,
  MessageIcon,
  ModuleIcon,
  PullRequestIcon,
  ThreadIcon,
  TicketIcon,
  UserIcon,
} from "./icons.tsx";

export const TYPE_COLOR: Record<NodeType, string> = {
  WorkItem: "var(--node-workitem)",
  Change: "var(--node-change)",
  Message: "var(--node-message)",
  Person: "var(--node-person)",
  Decision: "var(--node-decision)",
  Conversation: "var(--node-conversation)",
  Container: "var(--node-container)",
  Module: "var(--node-module)",
  Identity: "var(--node-identity)",
};

export const TYPE_ICON: Record<NodeType, typeof UserIcon> = {
  WorkItem: TicketIcon,
  Change: PullRequestIcon,
  Message: MessageIcon,
  Person: UserIcon,
  Decision: DecisionIcon,
  Conversation: ThreadIcon,
  Container: ContainerIcon,
  Module: ModuleIcon,
  Identity: IdentityIcon,
};
