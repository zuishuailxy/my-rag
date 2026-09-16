import { del, get, patch, post, put, request } from "./client";
import type {
  ChatMessage,
  ChatResult,
  ChatSession,
  ChunkHit,
  DocumentItem,
  GraphEdge,
  GraphHit,
  GraphOverview,
  GraphNode,
  LoginResult,
  PageResult,
  PermissionNode,
  ReviewTask,
  RoleItem,
  SearchHit,
  TeamItem,
  TeamTreeNode,
  UserStats,
  UserVO,
} from "../types";

export const authApi = {
  login: (username: string, password: string) =>
    post<LoginResult>("/auth/login", { username, password }),
  me: () => get<import("../types").AuthUser>("/auth/me"),
  logout: () => post<{ message: string }>("/auth/logout"),
};

export const documentApi = {
  list: (query: Record<string, string | number | undefined>) => {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== "") params.set(k, String(v));
    }
    const qs = params.toString();
    return get<PageResult<DocumentItem>>(`/documents${qs ? `?${qs}` : ""}`);
  },
  get: (id: string) => get<DocumentItem>(`/documents/${id}`),
  create: (body: Record<string, unknown>) =>
    post<DocumentItem>("/documents", body),
  update: (id: string, body: Record<string, unknown>) =>
    patch<DocumentItem>(`/documents/${id}`, body),
  remove: (id: string) => del<{ affected?: number }>(`/documents/${id}`),
  publish: (id: string) => put<DocumentItem>(`/documents/${id}/publish`),
  archive: (id: string) => put<DocumentItem>(`/documents/${id}/archive`),
  saveDraft: (id: string) => put<DocumentItem>(`/documents/${id}/save-draft`),
  uploadParse: (form: FormData) =>
    request<{ documentId: string; title: string; status: number }>(
      "/documents/upload/parse",
      {
        method: "POST",
        body: form,
      },
    ),
  reviewTasks: (query: Record<string, string | number | undefined>) => {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== "") params.set(k, String(v));
    }
    const qs = params.toString();
    return get<PageResult<ReviewTask>>(
      `/documents/reviews/tasks${qs ? `?${qs}` : ""}`,
    );
  },
  pendingCount: () => get<number>("/documents/reviews/tasks/pending-count"),
  approve: (taskId: string, reviewComment?: string) =>
    post<DocumentItem>(`/documents/reviews/tasks/${taskId}/approve`, {
      reviewComment,
    }),
  reject: (taskId: string, reviewComment: string) =>
    post<DocumentItem>(`/documents/reviews/tasks/${taskId}/reject`, {
      reviewComment,
    }),
};

export const searchApi = {
  search: (body: {
    keyword: string;
    page?: number;
    pageSize?: number;
    categoryId?: string;
    authorId?: string;
  }) => post<PageResult<SearchHit>>("/search", body),
};

export const aiApi = {
  chat: (content: string, topK = 5, sessionId?: string) =>
    post<ChatResult>("/ai/chat", { content, topK, sessionId }),
  ragSearch: (query: string, topK = 8) =>
    post<ChunkHit[]>("/rag/search", { query, topK }),
  sessions: (page = 1, pageSize = 50) =>
    get<PageResult<ChatSession>>(
      `/ai/sessions?page=${page}&pageSize=${pageSize}`,
    ),
  createSession: () => post<ChatSession>("/ai/sessions", {}),
  messages: (id: string) => get<ChatMessage[]>(`/ai/sessions/${id}/messages`),
  renameSession: (id: string, title: string) =>
    patch<ChatSession>(`/ai/sessions/${id}`, { title }),
  removeSession: (id: string) => del<{ message: string }>(`/ai/sessions/${id}`),
};

export const graphApi = {
  overview: (query: {
    keyword?: string;
    entityType?: string;
    from?: string;
    to?: string;
    docLimit?: number;
  }) => {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== "") params.set(k, String(v));
    }
    const qs = params.toString();
    return get<GraphOverview>(`/graph/overview${qs ? `?${qs}` : ""}`);
  },
  search: (keyword: string, limit = 50) => {
    const params = new URLSearchParams({ keyword, limit: String(limit) });
    return get<GraphHit[]>(`/graph/search?${params}`);
  },
  nodes: (type?: string, limit = 80) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (type) params.set("type", type);
    return get<GraphNode[]>(`/graph/nodes?${params}`);
  },
  edges: (limit = 120) => get<GraphEdge[]>(`/graph/edges?limit=${limit}`),
};

export const userApi = {
  stats: () => get<UserStats>("/users/me/stats"),
  updateMe: (body: { realName?: string; email?: string; avatar?: string }) =>
    put<UserVO>("/users/me", body),
  changePassword: (oldPassword: string, newPassword: string) =>
    put<{ message: string }>("/users/password/change", {
      oldPassword,
      newPassword,
    }),
  page: (query: Record<string, string | number | undefined>) => {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== "") params.set(k, String(v));
    }
    return get<PageResult<UserVO>>(`/users/page?${params}`);
  },
  create: (body: Record<string, unknown>) => post<UserVO>("/users", body),
  update: (id: string, body: Record<string, unknown>) =>
    put<UserVO>(`/users/${id}`, body),
  remove: (id: string) => del<{ message: string }>(`/users/${id}`),
  resetPassword: (id: string, newPassword: string) =>
    put<{ message: string }>(`/users/${id}/password/reset`, { newPassword }),
  getRoles: (id: string) =>
    get<{ userId: string; roleCodes: string[] }>(`/users/${id}/roles`),
  assignRoles: (id: string, roleCodes: string[]) =>
    put<{ userId: string; roleCodes: string[] }>(`/users/${id}/roles`, {
      roleCodes,
    }),
};

export const roleApi = {
  list: () => get<RoleItem[]>("/roles/list"),
  create: (body: {
    roleName: string;
    roleCode: string;
    description?: string;
  }) => post<RoleItem>("/roles", body),
  update: (id: string, body: Record<string, unknown>) =>
    put<RoleItem>(`/roles/${id}`, body),
  remove: (id: string) => del<{ message: string }>(`/roles/${id}`),
  permissions: (id: string) =>
    get<{ roleId: string; permissionIds: string[] }>(
      `/roles/${id}/permissions`,
    ),
  assignPermissions: (id: string, permissionIds: string[]) =>
    put<{ roleId: string; permissionIds: string[] }>(
      `/roles/${id}/permissions`,
      {
        permissionIds,
      },
    ),
};

export const permissionApi = {
  tree: () => get<PermissionNode[]>("/permissions/tree"),
};

export const teamApi = {
  page: (query: Record<string, string | number | undefined>) => {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== "") params.set(k, String(v));
    }
    return get<PageResult<TeamItem>>(`/teams/page?${params}`);
  },
  tree: () => get<TeamTreeNode[]>("/teams/tree"),
  mine: () => get<TeamItem[]>("/teams/mine"),
  create: (body: Record<string, unknown>) => post<TeamItem>("/teams", body),
  update: (id: string, body: Record<string, unknown>) =>
    put<TeamItem>(`/teams/${id}`, body),
  remove: (id: string) => del<{ message: string }>(`/teams/${id}`),
  members: (id: string) => get<unknown[]>(`/teams/${id}/members`),
  addMembers: (id: string, userIds: string[]) =>
    post<unknown>(`/teams/${id}/members`, userIds),
  removeMembers: (id: string, userIds: string[]) =>
    del<unknown>(`/teams/${id}/members`, userIds),
};
