import { useState, type ReactNode } from "react";
import { getToolName, isToolUIPart } from "ai";
import type { UIMessage } from "ai";
import { FileSearchOutlined, SearchOutlined } from "@ant-design/icons";
import { AnswerMarkdown, SourceCiteList } from "./SourceCiteList";
import type { ChatSource } from "../types";

export type RetrieveHit = {
  index: number;
  documentId: string;
  documentTitle: string;
  heading: string | null;
};

export type KhUIMessage = UIMessage<
  unknown,
  {
    status: { stage: string; text: string };
    think: { text: string };
    sources: ChatSource[];
    retrieve: { query: string; items: RetrieveHit[] };
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
  for (const part of parts) {
    if (part.type === "data-sources" && Array.isArray(part.data)) {
      return part.data;
    }
  }
  return [];
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
    .map((part) => part.text)
    .join("");
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
  const texts = parts.filter((part) => part.type === "text" && part.text);

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

  const hasRetrieve = parts.some((part) => part.type === "data-retrieve");

  return (
    <>
      {renderProcessParts(parts, hasRetrieve)}
      {texts.map((part, i) =>
        part.type === "text" ? (
          <div key={`text-${i}`} className="kh-bubble-md">
            <AnswerMarkdown
              text={part.text}
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

function renderProcessParts(parts: KhUIMessage["parts"], hasRetrieve: boolean) {
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
      nodes.push(
        <div key={i} className="kh-chat-status">
          {part.data.text}
        </div>,
      );
      return;
    }

    if (part.type === "data-retrieve") {
      nodes.push(
        <RetrieveCard
          key={i}
          query={part.data.query}
          items={part.data.items}
        />,
      );
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
  return nodes;
}

function RetrieveCard({
  query,
  items,
}: {
  query: string;
  items: RetrieveHit[];
}) {
  const count = items.length;
  const label = count ? "已检索可见知识库" : "未检索到你有权限的相关资料";

  return (
    <details className="kh-web">
      <summary>
        <FileSearchOutlined />
        <span className="kh-web-label">{label}</span>
        {query ? <span className="kh-web-q">{query}</span> : null}
        {count ? <span className="kh-web-n">{count}</span> : null}
      </summary>
      {count ? (
        <ul className="kh-web-list">
          {items.map((hit) => (
            <li key={`${hit.documentId}-${hit.index}`}>
              [{hit.index}] {hit.documentTitle}
              {hit.heading ? ` / ${hit.heading}` : ""}
            </li>
          ))}
        </ul>
      ) : null}
    </details>
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
    <details className="kh-think" open>
      <summary>{streaming ? "思考中…" : "思考过程"}</summary>
      <div className="kh-think-body">{text}</div>
    </details>
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
  const label = pending ? "正在搜索" : failed ? "搜索失败" : "已搜索";

  return (
    <details
      className={`kh-web${pending ? " pending" : ""}${failed ? " failed" : ""}`}
    >
      <summary>
        <SearchOutlined />
        <span className="kh-web-label">{label}</span>
        {query ? <span className="kh-web-q">{query}</span> : null}
        {!pending && count ? <span className="kh-web-n">{count}</span> : null}
      </summary>
      {failed ? (
        <div className="kh-web-err">{output?.error || part.errorText}</div>
      ) : count ? (
        <ul className="kh-web-list">
          {output?.items.map((hit) => (
            <li key={hit.url}>
              <a href={hit.url} target="_blank" rel="noreferrer">
                {hit.title}
              </a>
            </li>
          ))}
        </ul>
      ) : null}
    </details>
  );
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
