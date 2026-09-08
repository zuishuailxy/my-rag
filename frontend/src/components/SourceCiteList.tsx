import {
  Children,
  cloneElement,
  isValidElement,
  useCallback,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FileTypeIcon } from "./FileTypeIcon";

export interface CiteItem {
  index?: number;
  documentId: string;
  documentTitle: string;
  heading?: string | null;
  excerpt?: string;
}

export function citeAnchorId(scope: string, index: number) {
  return `kh-cite-${scope}-${index}`;
}

export function focusCite(scope: string, index: number) {
  const el = document.getElementById(citeAnchorId(scope, index));
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

/**
 * 引用卡片停在对话里，展示用到的块摘录。
 * 「查看原文」才开文档，避免点 [n] 就把问答冲掉。
 */
export function SourceCiteList({
  items,
  scope,
  activeIndex,
  onSelect,
}: {
  items: CiteItem[];
  scope: string;
  activeIndex?: number | null;
  onSelect?: (index: number) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="kh-cite-list">
      <div className="kh-cite-list-title">引用文档 ({items.length})</div>
      <div className="kh-cite-rail">
        {items.map((s, i) => {
          const index = s.index ?? i + 1;
          return (
            <div
              key={`${s.documentId}-${index}`}
              id={citeAnchorId(scope, index)}
              className={`kh-cite${activeIndex === index ? " active" : ""}`}
              onClick={() => onSelect?.(index)}
            >
              <FileTypeIcon name={s.documentTitle} size={28} />
              <div className="kh-cite-body">
                <div className="kh-cite-title" title={s.documentTitle}>
                  [{index}] {s.documentTitle}
                </div>
                {s.heading ? (
                  <div className="kh-cite-heading" title={s.heading}>
                    {s.heading}
                  </div>
                ) : null}
                {s.excerpt ? (
                  <div className="kh-cite-meta" title={s.excerpt}>
                    {s.excerpt}
                  </div>
                ) : null}
                <Link
                  className="kh-cite-open"
                  to={`/documents/${s.documentId}`}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => e.stopPropagation()}
                >
                  查看原文
                </Link>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 回答 Markdown + [n] 定位到本条消息的引用卡 */
export function AnswerMarkdown({
  text,
  sources,
  scope,
  onCite,
}: {
  text: string;
  sources?: CiteItem[];
  scope: string;
  onCite?: (index: number) => void;
}) {
  const byIndex = new Map(
    (sources ?? [])
      .filter((s) => s.index != null)
      .map((s) => [s.index as number, s]),
  );

  const handleCite = useCallback(
    (index: number) => {
      onCite?.(index);
      focusCite(scope, index);
    },
    [onCite, scope],
  );

  const wrap = (children: ReactNode) =>
    injectCites(children, byIndex, handleCite);

  return (
    <div className="kh-md">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p>{wrap(children)}</p>,
          li: ({ children }) => <li>{wrap(children)}</li>,
          strong: ({ children }) => <strong>{wrap(children)}</strong>,
          em: ({ children }) => <em>{wrap(children)}</em>,
          h1: ({ children }) => <h3>{wrap(children)}</h3>,
          h2: ({ children }) => <h3>{wrap(children)}</h3>,
          h3: ({ children }) => <h4>{wrap(children)}</h4>,
          h4: ({ children }) => <h4>{wrap(children)}</h4>,
          td: ({ children }) => <td>{wrap(children)}</td>,
          th: ({ children }) => <th>{wrap(children)}</th>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {wrap(children)}
            </a>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}

function injectCites(
  children: ReactNode,
  byIndex: Map<number, CiteItem>,
  onCite: (index: number) => void,
): ReactNode {
  return Children.map(children, (child, i) => {
    if (typeof child === "string" || typeof child === "number") {
      return (
        <CiteChips
          key={i}
          text={String(child)}
          byIndex={byIndex}
          onCite={onCite}
        />
      );
    }
    if (
      isValidElement<{ children?: ReactNode }>(child) &&
      child.props.children != null
    ) {
      return cloneElement(child, {
        children: injectCites(child.props.children, byIndex, onCite),
      });
    }
    return child;
  });
}

function CiteChips({
  text,
  byIndex,
  onCite,
}: {
  text: string;
  byIndex: Map<number, CiteItem>;
  onCite: (index: number) => void;
}) {
  const parts = text.split(/(\[\d+\])/);
  if (parts.length === 1) return text;
  return (
    <>
      {parts.map((part, i) => {
        const match = part.match(/^\[(\d+)\]$/);
        if (!match) return <span key={i}>{part}</span>;
        const index = Number(match[1]);
        if (!byIndex.has(index)) return <span key={i}>{part}</span>;
        return (
          <button
            key={i}
            type="button"
            className="kh-cite-inline"
            title="查看本条用到的资料块"
            onClick={() => onCite(index)}
          >
            {part}
          </button>
        );
      })}
    </>
  );
}
