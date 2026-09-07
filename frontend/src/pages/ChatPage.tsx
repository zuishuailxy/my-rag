import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Input, List, Space, Typography, message } from "antd";
import { aiApi } from "../api";
import { ApiError } from "../api/client";
import type { ChatSource } from "../types";

interface Bubble {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
}

export default function ChatPage() {
  const [input, setInput] = useState("");
  const [topK, setTopK] = useState(5);
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<Bubble[]>([]);
  const logRef = useRef<HTMLDivElement>(null);

  async function send(asRagOnly = false) {
    const text = input.trim();
    if (!text) return;
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setLoading(true);
    try {
      if (asRagOnly) {
        const hits = await aiApi.ragSearch(text, topK);
        const content = hits.length
          ? hits
              .map(
                (h, i) =>
                  `[${i + 1}] ${h.documentTitle}${h.heading ? ` / ${h.heading}` : ""}\n${h.content.slice(0, 180)}`,
              )
              .join("\n\n")
          : "没有召回到相关块。";
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `仅检索结果：\n\n${content}` },
        ]);
      } else {
        const res = await aiApi.chat(text, topK);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: res.answer, sources: res.sources },
        ]);
      }
      requestAnimationFrame(() => {
        logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
      });
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="kh-page kh-chat">
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        知识问答
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        走混合检索后再生成。无召回不会调模型。当前为一次性返回，不是 SSE。
      </Typography.Paragraph>
      <div className="kh-chat-log" ref={logRef}>
        {messages.map((m, i) => (
          <div key={i} className={`kh-bubble ${m.role}`}>
            {m.content}
            {m.sources?.length ? (
              <List
                size="small"
                style={{ marginTop: 8, background: "#fff", borderRadius: 8 }}
                dataSource={m.sources}
                renderItem={(s) => (
                  <List.Item>
                    <span>
                      [{s.index}]{" "}
                      <Link to={`/documents/${s.documentId}`}>
                        {s.documentTitle}
                      </Link>
                      {s.heading ? ` / ${s.heading}` : ""}
                      <div style={{ color: "#8c8c8c" }}>{s.excerpt}</div>
                    </span>
                  </List.Item>
                )}
              />
            ) : null}
          </div>
        ))}
      </div>
      <Space.Compact style={{ width: "100%" }}>
        <Input
          size="large"
          placeholder="例如：上线前如何做金丝雀验证？"
          value={input}
          disabled={loading}
          onChange={(e) => setInput(e.target.value)}
          onPressEnter={() => void send(false)}
        />
        <Input
          size="large"
          style={{ width: 80 }}
          value={topK}
          onChange={(e) => setTopK(Number(e.target.value) || 5)}
        />
        <Button size="large" loading={loading} onClick={() => void send(true)}>
          仅检索
        </Button>
        <Button
          type="primary"
          size="large"
          loading={loading}
          onClick={() => void send(false)}
        >
          发送
        </Button>
      </Space.Compact>
    </div>
  );
}
