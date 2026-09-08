import { useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Empty, Input, List, Space, Typography, message } from "antd";
import { aiApi } from "../api";
import { ApiError } from "../api/client";
import type { ChatMessage, ChatSession, ChatSource } from "../types";
import { formatTime } from "../utils";

interface Bubble {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[] | null;
}

export default function ChatPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const sessionId = params.get("session") || undefined;

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [input, setInput] = useState("");
  const [topK, setTopK] = useState(5);
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<Bubble[]>([]);
  const logRef = useRef<HTMLDivElement>(null);

  async function loadSessions() {
    try {
      const res = await aiApi.sessions();
      setSessions(res.items);
    } catch {
      /* 列表失败不挡问答 */
    }
  }

  useEffect(() => {
    loadSessions();
  }, []);

  useEffect(() => {
    if (!sessionId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    aiApi
      .messages(sessionId)
      .then((rows: ChatMessage[]) => {
        if (cancelled) return;
        setMessages(
          rows.map((m) => ({
            role: m.role,
            content: m.content,
            sources: m.sources,
          })),
        );
      })
      .catch((error) => {
        if (!cancelled) {
          message.error(
            error instanceof ApiError ? error.message : "加载会话失败",
          );
          navigate("/chat", { replace: true });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, navigate]);

  function scrollLog() {
    requestAnimationFrame(() => {
      logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
    });
  }

  async function send(asRagOnly = false) {
    const text = input.trim();
    if (!text) return;
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setLoading(true);
    scrollLog();
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
        const res = await aiApi.chat(text, topK, sessionId);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: res.answer, sources: res.sources },
        ]);
        if (res.sessionId && res.sessionId !== sessionId) {
          navigate(`/chat?session=${res.sessionId}`, { replace: true });
        }
        void loadSessions();
      }
      scrollLog();
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  async function onNew() {
    try {
      const created = await aiApi.createSession();
      navigate(`/chat?session=${created.id}`);
      void loadSessions();
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "创建失败");
    }
  }

  async function onRemove(id: string, e: MouseEvent) {
    e.stopPropagation();
    try {
      await aiApi.removeSession(id);
      if (sessionId === id) navigate("/chat");
      void loadSessions();
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "删除失败");
    }
  }

  return (
    <div className="kh-page kh-chat-layout">
      <aside className="kh-chat-sessions">
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          onClick={() => void onNew()}
        >
          新对话
        </Button>
        <div className="kh-chat-session-list">
          {sessions.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="还没有会话"
            />
          ) : (
            sessions.map((s) => (
              <div
                key={s.id}
                className={`kh-chat-session-item${sessionId === s.id ? " active" : ""}`}
                onClick={() => navigate(`/chat?session=${s.id}`)}
              >
                <div className="kh-chat-session-title">{s.title}</div>
                <div className="kh-chat-session-meta">
                  <span>{formatTime(s.updatedAt)}</span>
                  <DeleteOutlined onClick={(e) => void onRemove(s.id, e)} />
                </div>
              </div>
            ))
          )}
        </div>
      </aside>
      <div className="kh-chat-main">
        <Typography.Title level={4} style={{ marginTop: 0 }}>
          知识问答
        </Typography.Title>
        <Typography.Paragraph type="secondary">
          走混合检索后再生成。无召回不会调模型。问答会写入左侧会话，「仅检索」不落库。
        </Typography.Paragraph>
        <div className="kh-chat-log" ref={logRef}>
          {messages.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="输入问题开始一段对话"
            />
          ) : (
            messages.map((m, i) => (
              <div key={i} className={`kh-bubble ${m.role}`}>
                {m.content}
                {m.sources?.length ? (
                  <List
                    size="small"
                    style={{
                      marginTop: 8,
                      background: "#fff",
                      borderRadius: 8,
                    }}
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
            ))
          )}
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
          <Button
            size="large"
            loading={loading}
            onClick={() => void send(true)}
          >
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
    </div>
  );
}
