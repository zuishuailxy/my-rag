import { Link } from "react-router-dom";
import { FileTypeIcon } from "./FileTypeIcon";

export interface CiteItem {
  index?: number;
  documentId: string;
  documentTitle: string;
  heading?: string | null;
  excerpt?: string;
}

/**
 * RAG / 检索引用卡片。标题新开标签打开文档，避免问答页被冲掉。
 */
export function SourceCiteList({ items }: { items: CiteItem[] }) {
  if (!items.length) return null;
  return (
    <div className="kh-cite-list">
      {items.map((s, i) => (
        <Link
          key={`${s.documentId}-${s.index ?? i}`}
          className="kh-cite"
          to={`/documents/${s.documentId}`}
          target="_blank"
          rel="noreferrer"
        >
          <FileTypeIcon name={s.documentTitle} />
          <div className="kh-cite-body">
            <div className="kh-cite-title">
              {s.index != null ? `[${s.index}] ` : ""}
              {s.documentTitle}
            </div>
            {s.heading ? (
              <div className="kh-cite-heading">{s.heading}</div>
            ) : null}
            {s.excerpt ? (
              <div className="kh-cite-excerpt">{s.excerpt}</div>
            ) : null}
          </div>
        </Link>
      ))}
    </div>
  );
}

/** 把回答里的 [n] 做成指向对应文档的链接 */
export function AnswerWithCitations({
  text,
  sources,
}: {
  text: string;
  sources?: CiteItem[];
}) {
  const byIndex = new Map(
    (sources ?? [])
      .filter((s) => s.index != null)
      .map((s) => [s.index as number, s]),
  );
  const parts = text.split(/(\[\d+\])/);
  return (
    <>
      {parts.map((part, i) => {
        const match = part.match(/^\[(\d+)\]$/);
        if (!match) return <span key={i}>{part}</span>;
        const src = byIndex.get(Number(match[1]));
        if (!src) return <span key={i}>{part}</span>;
        return (
          <Link
            key={i}
            className="kh-cite-inline"
            to={`/documents/${src.documentId}`}
            target="_blank"
            rel="noreferrer"
          >
            {part}
          </Link>
        );
      })}
    </>
  );
}
