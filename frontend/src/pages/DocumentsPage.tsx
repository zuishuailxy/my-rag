import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Checkbox,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Upload,
  message,
} from "antd";
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
import { FileTypeIcon, fileTypeLabel } from "../components/FileTypeIcon";

export default function DocumentsPage() {
  const user = useAuth();
  const navigate = useNavigate();
  const [title, setTitle] = useState("");
  const [status, setStatus] = useState<number | undefined>();
  const [mineOnly, setMineOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<DocumentItem[]>([]);
  const [loading, setLoading] = useState(false);
  const pageSize = 10;

  async function load(nextPage = page) {
    setLoading(true);
    try {
      const res = await documentApi.list({
        page: nextPage,
        pageSize,
        title: title.trim() || undefined,
        status,
        authorId: mineOnly ? user?.userId : undefined,
      });
      setItems(res.items);
      setTotal(res.total);
      setPage(nextPage);
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(1);
  }, []);

  return (
    <div className="kh-page">
      <p className="kh-access-hint">
        列表只展示你能看的文档：公开、所在团队，以及自己写的。编辑 /
        发布仅作者或管理员可用。
      </p>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          allowClear
          placeholder="标题搜索"
          style={{ width: 240 }}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onPressEnter={() => void load(1)}
        />
        <Select
          allowClear
          placeholder="状态"
          style={{ width: 140 }}
          value={status}
          onChange={setStatus}
          options={Object.entries(DOC_STATUS).map(([k, v]) => ({
            value: Number(k),
            label: v.label,
          }))}
        />
        <Checkbox
          checked={mineOnly}
          onChange={(e) => setMineOnly(e.target.checked)}
        >
          仅我的
        </Checkbox>
        <Button onClick={() => void load(1)}>查询</Button>
        {can(user, "document:create") ? (
          <>
            <Button type="primary" onClick={() => navigate("/documents/new")}>
              新建文档
            </Button>
            <Upload
              showUploadList={false}
              beforeUpload={async (file) => {
                const form = new FormData();
                form.append("file", file);
                try {
                  const res = await documentApi.uploadParse(form);
                  message.success("已解析为草稿，可在编辑页设置公开或团队");
                  navigate(`/documents/${res.documentId}/edit`);
                } catch (error) {
                  message.error(
                    error instanceof ApiError ? error.message : "上传失败",
                  );
                }
                return false;
              }}
            >
              <Button>上传解析</Button>
            </Upload>
          </>
        ) : null}
      </Space>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={items}
        pagination={{
          current: page,
          pageSize,
          total,
          onChange: (p) => void load(p),
        }}
        columns={[
          {
            title: "标题",
            dataIndex: "title",
            render: (value: string, row: DocumentItem) => (
              <a
                onClick={() => navigate(`/documents/${row.id}`)}
                style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
              >
                <FileTypeIcon name={row.title} />
                {value}
              </a>
            ),
          },
          {
            title: "文件类型",
            dataIndex: "title",
            width: 100,
            render: (title: string) => fileTypeLabel(title),
          },
          {
            title: "状态",
            dataIndex: "status",
            width: 100,
            render: (s: number) => (
              <Tag color={DOC_STATUS[s]?.color}>{DOC_STATUS[s]?.label}</Tag>
            ),
          },
          {
            title: "可见性",
            width: 110,
            render: (_: unknown, row: DocumentItem) => {
              const vis = visibilityMeta(row);
              return <Tag color={vis.color}>{vis.label}</Tag>;
            },
          },
          {
            title: "更新时间",
            dataIndex: "updatedAt",
            width: 180,
            render: formatTime,
          },
          {
            title: "操作",
            width: 160,
            render: (_: unknown, row: DocumentItem) => {
              const writable =
                can(user, "document:edit") && canWriteDocument(user, row);
              return (
                <Space>
                  {writable ? (
                    <a onClick={() => navigate(`/documents/${row.id}/edit`)}>
                      编辑
                    </a>
                  ) : null}
                  {writable && row.status === 0 ? (
                    <a
                      onClick={async () => {
                        try {
                          await documentApi.publish(row.id);
                          message.success("已提交发布");
                          void load();
                        } catch (error) {
                          message.error(
                            error instanceof ApiError
                              ? error.message
                              : "发布失败",
                          );
                        }
                      }}
                    >
                      发布
                    </a>
                  ) : null}
                </Space>
              );
            },
          },
        ]}
      />
    </div>
  );
}
