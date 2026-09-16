import type { AuthUser, TeamItem, TeamTreeNode } from "./types";

export const DOC_STATUS: Record<number, { label: string; color: string }> = {
  0: { label: "草稿", color: "default" },
  1: { label: "已发布", color: "success" },
  2: { label: "已归档", color: "warning" },
  3: { label: "待审核", color: "processing" },
};

export function formatTime(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function can(user: AuthUser | null, code: string) {
  if (!user) return false;
  if (user.roles.includes("ROLE_ADMIN")) return true;
  return user.permissions.includes(code);
}

export function isAdmin(user: AuthUser | null) {
  return Boolean(user?.roles.includes("ROLE_ADMIN"));
}

export function isReviewer(user: AuthUser | null) {
  return Boolean(
    user?.roles.includes("ROLE_ADMIN") || user?.roles.includes("ROLE_REVIEWER"),
  );
}

/** 作者或管理员可改文档 */
export function canWriteDocument(
  user: AuthUser | null,
  doc: { authorId?: string | null },
) {
  if (!user) return false;
  if (isAdmin(user)) return true;
  return Boolean(doc.authorId && doc.authorId === user.userId);
}

export function visibilityMeta(doc: {
  isPublic?: boolean | null;
  teamId?: string | null;
}) {
  if (doc.isPublic) return { label: "公开", color: "success" as const };
  if (doc.teamId) return { label: "团队可见", color: "blue" as const };
  return { label: "仅自己", color: "default" as const };
}

export function flattenTeams(nodes: TeamTreeNode[] | unknown[]): TeamItem[] {
  const out: TeamItem[] = [];
  const walk = (list: unknown[]) => {
    for (const raw of list) {
      if (!raw || typeof raw !== "object") continue;
      const node = raw as TeamTreeNode;
      if (node.id && node.teamName) out.push(node);
      if (Array.isArray(node.children) && node.children.length)
        walk(node.children);
    }
  };
  walk(nodes);
  return out;
}

export function displayName(user: AuthUser | null) {
  if (!user) return "未登录";
  return user.realName?.trim() || user.username;
}

/** ES 高亮只保留 em，避免 XSS */
export function safeHighlight(html?: string) {
  if (!html) return "";
  return html
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/&lt;em&gt;/g, "<em>")
    .replace(/&lt;\/em&gt;/g, "</em>");
}
