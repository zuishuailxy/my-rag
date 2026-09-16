import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Button,
  Empty,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { documentApi } from "../api";
import { ApiError } from "../api/client";
import type { DocumentItem } from "../types";
import { useAuth } from "../auth";
import {
  DOC_STATUS,
  can,
  canWriteDocument,
  formatTime,
  visibilityMeta,
} from "../utils";
import { FileTypeIcon } from "../components/FileTypeIcon";

export default function DocumentDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const user = useAuth();
  const [doc, setDoc] = useState<DocumentItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);

  async function load() {
    setLoading(true);
    setForbidden(false);
    try {
      setDoc(await documentApi.get(id));
    } catch (error) {
      setDoc(null);
      if (error instanceof ApiError && error.status === 403) {
        setForbidden(true);
        message.error("无权查看该文档");
      } else {
        message.error(error instanceof ApiError ? error.message : "加载失败");
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, [id]);

  if (loading) {
    return (
      <div className="kh-page">
        <Spin />
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="kh-page">
        <Empty
          description={forbidden ? "无权查看该文档" : "文档不存在或已删除"}
        >
          <Button onClick={() => navigate("/documents")}>返回列表</Button>
        </Empty>
      </div>
    );
  }

  const status = DOC_STATUS[doc.status];
  const vis = visibilityMeta(doc);
  const writable = canWriteDocument(user, doc);

  return (
    <div className="kh-page">
      <Space style={{ marginBottom: 12 }} wrap>
        <Button onClick={() => navigate("/documents")}>返回列表</Button>
        {can(user, "document:edit") && writable ? (
          <Button onClick={() => navigate(`/documents/${id}/edit`)}>
            编辑
          </Button>
        ) : null}
        {can(user, "document:edit") &&
        writable &&
        (doc.status === 0 || doc.status === 2) ? (
          <Button
            type="primary"
            onClick={async () => {
              try {
                setDoc(await documentApi.publish(id));
                message.success("已发布（若开启审核则进入待审）");
              } catch (error) {
                message.error(
                  error instanceof ApiError ? error.message : "发布失败",
                );
              }
            }}
          >
            发布
          </Button>
        ) : null}
        {can(user, "document:edit") && writable && doc.status === 1 ? (
          <>
            <Button
              onClick={async () => {
                try {
                  setDoc(await documentApi.saveDraft(id));
                  message.success("已下架为草稿");
                } catch (error) {
                  message.error(
                    error instanceof ApiError ? error.message : "操作失败",
                  );
                }
              }}
            >
              下架编辑
            </Button>
            <Button
              onClick={async () => {
                try {
                  setDoc(await documentApi.archive(id));
                  message.success("已归档");
                } catch (error) {
                  message.error(
                    error instanceof ApiError ? error.message : "归档失败",
                  );
                }
              }}
            >
              归档
            </Button>
          </>
        ) : null}
        {can(user, "document:delete") && writable ? (
          <Popconfirm
            title="确认删除该文档？"
            onConfirm={async () => {
              try {
                await documentApi.remove(id);
                message.success("已删除");
                navigate("/documents");
              } catch (error) {
                message.error(
                  error instanceof ApiError ? error.message : "删除失败",
                );
              }
            }}
          >
            <Button danger>删除</Button>
          </Popconfirm>
        ) : null}
      </Space>
      <Typography.Title
        level={3}
        style={{
          marginBottom: 8,
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <FileTypeIcon name={doc.title} size={28} />
        {doc.title}
      </Typography.Title>
      <Space wrap>
        <Tag color={status?.color}>{status?.label}</Tag>
        <Tag color={vis.color}>{vis.label}</Tag>
        <span style={{ color: "#8c8c8c" }}>
          更新于 {formatTime(doc.updatedAt)}
        </span>
      </Space>
      {doc.summary ? (
        <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
          {doc.summary}
        </Typography.Paragraph>
      ) : null}
      <div style={{ marginTop: 16 }}>
        <Markdown remarkPlugins={[remarkGfm]}>{doc.content || ""}</Markdown>
      </div>
    </div>
  );
}
