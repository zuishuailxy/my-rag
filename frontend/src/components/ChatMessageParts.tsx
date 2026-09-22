import { useState, type ReactNode } from "react";
import { getToolName, isToolUIPart } from "ai";
import type { UIMessage } from "ai";
import { AnswerMarkdown, SourceCiteList } from "./SourceCiteList";
import type { ChatSource } from "../types";

export type RetrieveHit = {
  index: number;
  documentId: string;
  documentTitle: string;
  heading: string | null;
};

/** 与后端 ChatIntent 一致；kb = 知识库 */
export type ChatIntent =
  | "chitchat" // 闲聊，不检索
  | "profile" // 个人偏好，不检索
  | "kb" // 知识库
  | "web" // 联网
  | "kb_then_web"; // 先知识库，不足再联网

/** data-intent：本轮路由，决定是否展示知识库/联网范围 */
export type IntentPart = {
  intent: ChatIntent;
  label: string;
  query: string;
  allowRetrieve: boolean;
  allowWeb: boolean;
};

/** data-eval：ok 表示资料切题，不是「有召回条数」 */
export type EvalPart = {
  ok: boolean;
  reason:
    | "ok" // 首轮检索切题
    | "retried_ok" // 改写后再查切题
    | "empty" // 首轮无命中
    | "retried_empty" // 改写后再查仍无命中
    | "irrelevant" // 首轮有命中但不切题
    | "retried_irrelevant" // 改写后再查仍不切题
    | "error"; // 检索接口失败
  text: string;
  retried: boolean;
  query: string;
  retryQuery?: string;
};

export type KhUIMessage = UIMessage<
  unknown,
  {
    status: { stage: string; text: string };
    think: { text: string };
    sources: ChatSource[];
    intent: IntentPart;
    eval: EvalPart;
    session: { sessionId: string };
  }
>;

type WebSearchHit = {
  title: string;
  url: string;
  snippet: string;
  siteName?: string;
};

type WebSearchResult = {
  query: string;
  items: WebSearchHit[];
  error?: string;
};

export function sourcesFromParts(parts: KhUIMessage["parts"]): ChatSource[] {
  // ⚠️ 后端每次检索都推**全量** data-sources（agentic 循环最多两次检索 = 多个 data part）；
  // AI SDK 对**无 id** 的 data part 是 append 而非 replace，所以这里取**最后一个**快照。
  // 取第一个会拿到首轮「不切题」时空的 []，导致引用区不显示。
  let latest: ChatSource[] = [];
  for (const part of parts) {
    if (part.type === "data-sources" && Array.isArray(part.data)) {
      latest = part.data;
    }
  }
  return latest;
}

/** 只保留回答里实际标了 [n] 的资料，避免无关召回也占引用区 */
export function citedSources(
  sources: ChatSource[],
  answer: string,
): ChatSource[] {
  const used = new Set(
    [...answer.matchAll(/\[(\d+)\]/g)].map((m) => Number(m[1])),
  );
  if (!used.size) return [];
  return sources.filter((s) => used.has(s.index));
}

export function textFromParts(parts: KhUIMessage["parts"]): string {
  return parts
    .filter(
      (part): part is { type: "text"; text: string } => part.type === "text",
    )
    .map((part) => stripInternalToolJson(part.text))
    .filter(Boolean)
    .join("");
}

/** 评估/改写模型的 JSON 不应出现在回答里 */
function stripInternalToolJson(text: string): string {
  return text
    .split("\n")
    .filter((line) => !isInternalToolJson(line.trim()))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function isInternalToolJson(text: string): boolean {
  if (!text.startsWith("{")) return false;
  try {
    const obj = JSON.parse(text) as Record<string, unknown>;
    if (obj == null || typeof obj !== "object" || Array.isArray(obj))
      return false;
    const keys = Object.keys(obj);
    if (keys.includes("relevant") && keys.includes("reason")) return true;
    if (keys.length === 1 && keys[0] === "query") return true;
    if (keys.includes("intent") && keys.includes("standalone_query"))
      return true;
    return false;
  } catch {
    return false;
  }
}

export function historyToUIMessages(
  rows: Array<{
    id: string;
    role: "user" | "assistant";
    content: string;
    sources?: ChatSource[] | null;
  }>,
): KhUIMessage[] {
  return rows.map((row) => {
    if (row.role === "user") {
      return {
        id: row.id,
        role: "user",
        parts: [{ type: "text", text: row.content }],
      };
    }
    const parts: KhUIMessage["parts"] = [];
    if (row.sources?.length) {
      parts.push({ type: "data-sources", data: row.sources });
    }
    parts.push({ type: "text", text: row.content });
    return { id: row.id, role: "assistant", parts };
  });
}

export function ChatMessageParts({
  messageId,
  parts,
  role,
  showSources = true,
}: {
  messageId: string;
  parts: KhUIMessage["parts"];
  role: KhUIMessage["role"];
  showSources?: boolean;
}) {
  const sources = citedSources(sourcesFromParts(parts), textFromParts(parts));
  const [activeCite, setActiveCite] = useState<number | null>(null);
  const texts = parts.filter((part) => {
    if (part.type !== "text" || !part.text) return false;
    return Boolean(stripInternalToolJson(part.text));
  });

  if (role === "user") {
    return (
      <>
        {texts.map((part, i) =>
          part.type === "text" ? (
            <div key={i} className="kh-bubble-text">
              {part.text}
            </div>
          ) : null,
        )}
      </>
    );
  }

  const hasRetrieve = parts.some(
    (part) => isToolUIPart(part) && getToolName(part) === "retrieve_knowledge",
  );
  const hasIntent = parts.some((part) => part.type === "data-intent");
  const hasRewrite = parts.some(
    (part) => isToolUIPart(part) && getToolName(part) === "rewrite_query",
  );

  return (
    <>
      {renderProcessParts(parts, hasRetrieve, hasIntent, hasRewrite)}
      {texts.map((part, i) =>
        part.type === "text" ? (
          <div key={`text-${i}`} className="kh-bubble-md">
            <AnswerMarkdown
              text={stripInternalToolJson(part.text)}
              sources={sources}
              scope={messageId}
              onCite={setActiveCite}
            />
          </div>
        ) : null,
      )}
      {showSources && sources.length ? (
        <SourceCiteList
          items={sources}
          scope={messageId}
          activeIndex={activeCite}
          onSelect={setActiveCite}
        />
      ) : null}
    </>
  );
}

/** 回答正文前的过程条：意图 → 检索 → 评估 → 改写 → 再检索 → 联网 → 思考。 */
function renderProcessParts(
  parts: KhUIMessage["parts"],
  hasRetrieve: boolean,
  hasIntent: boolean,
  hasRewrite: boolean,
) {
  const nodes: ReactNode[] = [];
  let reasoningBuf: string[] = [];
  let reasoningStreaming = false;
  let thinkKey = 0;

  const flushReasoning = () => {
    if (!reasoningBuf.length) return;
    nodes.push(
      <ThinkBlock
        key={`think-${thinkKey}`}
        text={reasoningBuf.join("\n\n")}
        streaming={reasoningStreaming}
      />,
    );
    thinkKey += 1;
    reasoningBuf = [];
    reasoningStreaming = false;
  };

  parts.forEach((part, i) => {
    if (part.type === "reasoning" || part.type === "data-think") {
      const text =
        "text" in part && typeof part.text === "string" ? part.text : "";
      if (text) reasoningBuf.push(text);
      if ("state" in part && part.state === "streaming")
        reasoningStreaming = true;
      return;
    }

    flushReasoning();

    if (
      part.type === "step-start" ||
      part.type === "data-session" ||
      part.type === "data-sources" ||
      part.type === "source-document" ||
      part.type === "text"
    ) {
      return;
    }

    if (part.type === "data-status") {
      if (part.data.stage === "generate") return;
      if (part.data.stage === "retrieve" && hasRetrieve) return;
      if (part.data.stage === "intent" && hasIntent) return;
      if (part.data.stage === "rewrite" && hasRewrite) return;
      nodes.push(
        <TraceItem key={i} kind="status" tone="pending">
          <div className="kh-trace-status">{part.data.text}</div>
        </TraceItem>,
      );
      return;
    }

    if (part.type === "data-intent") {
      nodes.push(<IntentCard key={i} data={part.data} />);
      return;
    }

    if (part.type === "data-eval") {
      nodes.push(<EvalCard key={i} data={part.data} />);
      return;
    }

    if (isToolUIPart(part) && getToolName(part) === "retrieve_knowledge") {
      nodes.push(<RetrieveToolCard key={i} part={part} />);
      return;
    }

    if (isToolUIPart(part) && getToolName(part) === "rewrite_query") {
      nodes.push(<RewriteToolCard key={i} part={part} />);
      return;
    }

    if (part.type === "source-url") {
      nodes.push(
        <a
          key={i}
          className="kh-web-link"
          href={part.url}
          target="_blank"
          rel="noreferrer"
        >
          {part.title || part.url}
        </a>,
      );
      return;
    }

    if (isToolUIPart(part) && getToolName(part) === "web_search") {
      nodes.push(<WebSearchCard key={i} part={part} />);
    }
  });

  flushReasoning();
  if (!nodes.length) return null;
  return <div className="kh-trace">{nodes}</div>;
}

function TraceItem({
  kind,
  tone,
  children,
}: {
  kind: string;
  tone?: string;
  children: ReactNode;
}) {
  return (
    <div className={`kh-trace-item kh-trace-${kind}${tone ? ` ${tone}` : ""}`}>
      <span className="kh-trace-node" aria-hidden />
      <div className="kh-trace-card">{children}</div>
    </div>
  );
}

function IntentCard({ data }: { data: IntentPart }) {
  return (
    <TraceItem kind="intent">
      <div className="kh-panel">
        <div className="kh-panel-head">
          <span className="kh-panel-type">意图识别</span>
          <span className="kh-panel-title">{data.label}</span>
        </div>
        <dl className="kh-meta">
          {data.query ? (
            <div>
              <dt>建议检索词</dt>
              <dd>{data.query}</dd>
            </div>
          ) : null}
          <div>
            <dt>范围</dt>
            <dd>
              <span className={data.allowRetrieve ? "on" : "off"}>知识库</span>
              <span className={data.allowWeb ? "on" : "off"}>联网</span>
            </dd>
          </div>
        </dl>
      </div>
    </TraceItem>
  );
}

function EvalCard({ data }: { data: EvalPart }) {
  const tone = !data.ok ? "failed" : data.retried ? "warn" : "ok";
  const stamp = !data.ok ? "不足" : data.retried ? "已改写" : "切题";
  return (
    <TraceItem kind="eval" tone={tone}>
      <div className="kh-panel">
        <div className="kh-panel-head">
          <span className="kh-panel-type">检索评估</span>
          <span className={`kh-badge ${tone}`}>{stamp}</span>
          <span className="kh-panel-title">{data.text}</span>
        </div>
        {data.query || data.retryQuery ? (
          <dl className="kh-meta">
            {data.query ? (
              <div>
                <dt>检索词</dt>
                <dd>{data.query}</dd>
              </div>
            ) : null}
            {data.retryQuery ? (
              <div>
                <dt>改写词</dt>
                <dd>{data.retryQuery}</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </div>
    </TraceItem>
  );
}

function RetrieveHits({ items }: { items: RetrieveHit[] }) {
  return (
    <ol className="kh-sheet-docs">
      {items.map((hit) => (
        <li key={`${hit.documentId}-${hit.index}`}>
          <em>{String(hit.index).padStart(2, "0")}</em>
          <span>
            {hit.documentTitle}
            {hit.heading ? <small>{hit.heading}</small> : null}
          </span>
        </li>
      ))}
    </ol>
  );
}

function ThinkBlock({
  text,
  streaming,
}: {
  text: string;
  streaming?: boolean;
}) {
  return (
    <TraceItem kind="think" tone={streaming ? "pending" : undefined}>
      <details className="kh-note" open>
        <summary className="kh-panel-head">
          <span className="kh-panel-type">思考过程</span>
          <span className="kh-panel-title">
            {streaming ? "思考中…" : "已完成"}
          </span>
        </summary>
        <p className="kh-note-body">{text}</p>
      </details>
    </TraceItem>
  );
}

function KbPanel({
  pending,
  failed,
  label,
  query,
  count,
  error,
  children,
}: {
  pending: boolean;
  failed: boolean;
  label: string;
  query?: string;
  count?: number;
  error?: string;
  children?: ReactNode;
}) {
  const tone = pending ? "pending" : failed ? "failed" : "ok";
  return (
    <TraceItem kind="kb" tone={tone}>
      <details className="kh-sheet" open>
        <summary className="kh-panel-head">
          <span className="kh-panel-type">知识库</span>
          <span className="kh-panel-title">{label}</span>
          {query ? <span className="kh-panel-sub">{query}</span> : null}
          {count ? <b className="kh-panel-n">{count}</b> : null}
        </summary>
        {error ? <div className="kh-step-err">{error}</div> : children}
      </details>
    </TraceItem>
  );
}

function RetrieveToolCard({
  part,
}: {
  part: {
    state: string;
    input?: unknown;
    output?: unknown;
    errorText?: string;
  };
}) {
  const input = asRecord(part.input);
  const pending =
    part.state === "input-streaming" || part.state === "input-available";
  const rec = parseToolPayload(part.output);
  const query =
    (typeof rec?.query === "string" && rec.query) ||
    (typeof input.query === "string" ? input.query : "");
  const items = Array.isArray(rec?.items) ? (rec.items as RetrieveHit[]) : [];
  const failed =
    part.state === "output-error" || typeof rec?.error === "string";
  const label = pending
    ? "正在检索知识库"
    : failed
      ? "检索失败"
      : items.length
        ? "已检索可见文档"
        : "未检索到相关资料";

  return (
    <KbPanel
      pending={pending}
      failed={failed || !items.length}
      label={label}
      query={query}
      count={!pending && items.length ? items.length : undefined}
      error={
        failed ? String(rec?.error || part.errorText || "检索失败") : undefined
      }
    >
      {!failed && items.length ? <RetrieveHits items={items} /> : null}
    </KbPanel>
  );
}

function RewriteToolCard({
  part,
}: {
  part: {
    state: string;
    input?: unknown;
    output?: unknown;
    errorText?: string;
  };
}) {
  const rec = parseToolPayload(part.output);
  const pending =
    part.state === "input-streaming" || part.state === "input-available";
  const failed =
    part.state === "output-error" || typeof rec?.error === "string";
  const query = typeof rec?.query === "string" ? rec.query : "";
  const previous =
    typeof rec?.previousQuery === "string" ? rec.previousQuery : "";
  const tone = pending ? "pending" : failed ? "failed" : "ok";
  const title = pending
    ? "正在改写检索词"
    : failed
      ? "改写失败"
      : "已改写检索词";

  return (
    <TraceItem kind="rewrite" tone={tone}>
      <div className="kh-panel">
        <div className="kh-panel-head">
          <span className="kh-panel-type">改写</span>
          <span className="kh-panel-title">{title}</span>
        </div>
        {failed ? (
          <div className="kh-step-err">
            {String(rec?.error || part.errorText || "改写失败")}
          </div>
        ) : previous || query ? (
          <dl className="kh-meta">
            {previous ? (
              <div>
                <dt>上次</dt>
                <dd>{previous}</dd>
              </div>
            ) : null}
            {query ? (
              <div>
                <dt>新检索词</dt>
                <dd>{query}</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </div>
    </TraceItem>
  );
}

function WebSearchCard({
  part,
}: {
  part: {
    state: string;
    input?: unknown;
    output?: unknown;
    errorText?: string;
  };
}) {
  const input = asRecord(part.input);
  const query = typeof input.query === "string" ? input.query : "";
  const pending =
    part.state === "input-streaming" || part.state === "input-available";
  const output = asWebSearchResult(part.output);
  const failed = part.state === "output-error" || Boolean(output?.error);
  const count = output?.items?.length ?? 0;
  const label = pending
    ? "正在搜索公开信息"
    : failed
      ? "搜索失败"
      : "已搜索公开信息";
  const tone = pending
    ? "pending"
    : failed
      ? "failed"
      : count
        ? "ok"
        : undefined;

  return (
    <TraceItem kind="web" tone={tone}>
      <details className="kh-links" open={pending || failed || !count}>
        <summary className="kh-panel-head">
          <span className="kh-panel-type">联网</span>
          <span className="kh-panel-title">{label}</span>
          {query ? <span className="kh-panel-sub">{query}</span> : null}
          {!pending && count ? <b className="kh-panel-n">{count}</b> : null}
        </summary>
        {failed ? (
          <div className="kh-step-err">{output?.error || part.errorText}</div>
        ) : count ? (
          <div className="kh-link-grid">
            {output?.items.map((hit) => {
              const host = hostOf(hit.url);
              return (
                <a
                  key={hit.url}
                  className="kh-link-tile"
                  href={hit.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span className="kh-link-fav">
                    {host.slice(0, 1).toUpperCase()}
                  </span>
                  <span className="kh-link-title">{hit.title}</span>
                  <span className="kh-link-host">{host}</span>
                </a>
              );
            })}
          </div>
        ) : null}
      </details>
    </TraceItem>
  );
}

function hostOf(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object")
    return value as Record<string, unknown>;
  return {};
}

function asWebSearchResult(value: unknown): WebSearchResult | null {
  const rec = parseToolPayload(value);
  if (!rec) return null;
  return {
    query: typeof rec.query === "string" ? rec.query : "",
    items: Array.isArray(rec.items) ? (rec.items as WebSearchHit[]) : [],
    error: typeof rec.error === "string" ? rec.error : undefined,
  };
}

/** LangChain tool 结果经常是 JSON 字符串或带 content 的消息对象 */
function parseToolPayload(value: unknown): Record<string, unknown> | null {
  if (typeof value === "string") {
    try {
      return parseToolPayload(JSON.parse(value));
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== "object") return null;
  const rec = value as Record<string, unknown>;
  if (typeof rec.content === "string") {
    const nested = parseToolPayload(rec.content);
    if (nested) return nested;
  }
  return rec;
}
