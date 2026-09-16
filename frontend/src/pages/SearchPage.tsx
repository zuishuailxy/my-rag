import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ClockCircleOutlined,
  FolderOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import {
  Button,
  Empty,
  Input,
  Pagination,
  Select,
  Space,
  Tag,
  message,
} from "antd";
import { searchApi } from "../api";
import { ApiError } from "../api/client";
import type { SearchHit } from "../types";
import {
  DOC_STATUS,
  formatTime,
  safeHighlight,
  visibilityMeta,
} from "../utils";
import { FileTypeIcon } from "../components/FileTypeIcon";

export default function SearchPage() {
  const navigate = useNavigate();
  const [keyword, setKeyword] = useState("");
  const [categoryId, setCategoryId] = useState<string>();
  const [status, setStatus] = useState<number | undefined>();
  const [page, setPage] = useState(1);
  const [pageSize] = useState(10);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState<number | null>(null);

  async function runSearch(nextPage = 1) {
    const q = keyword.trim();
    if (!q) {
      message.warning("请输入关键词");
      return;
    }
    setLoading(true);
    const started = performance.now();
    try {
      const res = await searchApi.search({
        keyword: q,
        page: nextPage,
        pageSize,
        categoryId: categoryId || undefined,
      });
      const filtered =
        status === undefined
          ? res.items
          : res.items.filter((x) => x.status === status);
      setItems(filtered);
      setTotal(status === undefined ? res.total : filtered.length);
      setPage(nextPage);
      setElapsed((performance.now() - started) / 1000);
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "搜索失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="kh-page">
      <p className="kh-access-hint">
        只会检索你有权限的已发布文档：公开、所在团队，以及自己写的。
      </p>
      <div className="kh-search-bar">
        <Input
          size="large"
          allowClear
          prefix={<SearchOutlined style={{ color: "#bfbfbf" }} />}
          placeholder="输入关键词，检索你有权限的文档"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onPressEnter={() => void runSearch(1)}
        />
        <Button
          type="primary"
          size="large"
          loading={loading}
          onClick={() => void runSearch(1)}
        >
          搜索
        </Button>
      </div>
      <div className="kh-filters">
        <Input
          allowClear
          style={{ width: 180 }}
          placeholder="分类 ID"
          value={categoryId}
          onChange={(e) => setCategoryId(e.target.value || undefined)}
        />
        <Select
          allowClear
          style={{ width: 160 }}
          placeholder="文档状态"
          value={status}
          onChange={setStatus}
          options={[
            { value: 1, label: "已发布" },
            { value: 0, label: "草稿" },
            { value: 3, label: "待审核" },
            { value: 2, label: "已归档" },
          ]}
        />
        <Button
          onClick={() => {
            setCategoryId(undefined);
            setStatus(undefined);
          }}
        >
          清空
        </Button>
      </div>

      {elapsed !== null ? (
        <div className="kh-result-meta">
          <span>
            找到约 {total} 条可见结果（用时 {elapsed.toFixed(2)} 秒）
          </span>
          <span>相关度排序</span>
        </div>
      ) : null}

      {!items.length && elapsed !== null ? (
        <Empty description="没有匹配的可见文档" />
      ) : null}

      {items.map((hit) => {
        const titleHtml = hit.highlight.title[0] || hit.title;
        const snippet =
          hit.highlight.content[0] ||
          hit.highlight.summary[0] ||
          hit.summary ||
          "";
        const statusMeta =
          hit.status != null ? DOC_STATUS[hit.status] : undefined;
        const vis = visibilityMeta(hit);
        return (
          <div className="kh-hit" key={hit.id}>
            <div className="kh-hit-icon">
              <FileTypeIcon name={hit.title} size={28} />
            </div>
            <div style={{ flex: 1 }}>
              <div
                className="kh-hit-title"
                onClick={() => navigate(`/documents/${hit.id}`)}
                dangerouslySetInnerHTML={{ __html: safeHighlight(titleHtml) }}
              />
              {snippet ? (
                <div
                  className="kh-hit-snippet"
                  dangerouslySetInnerHTML={{ __html: safeHighlight(snippet) }}
                />
              ) : null}
              <div className="kh-hit-meta">
                <span>
                  <FolderOutlined /> 来自文档库
                </span>
                <span>
                  <ClockCircleOutlined /> 更新时间:{" "}
                  {formatTime(hit.publishTime)}
                </span>
                {statusMeta ? (
                  <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
                ) : null}
                <Tag color={vis.color}>{vis.label}</Tag>
                <Space size={4}>
                  {(hit.tags || "")
                    .split(",")
                    .map((t) => t.trim())
                    .filter(Boolean)
                    .map((t) => (
                      <Tag key={t}>{t}</Tag>
                    ))}
                </Space>
              </div>
            </div>
          </div>
        );
      })}

      {total > pageSize ? (
        <div style={{ marginTop: 16, textAlign: "right" }}>
          <Pagination
            current={page}
            pageSize={pageSize}
            total={total}
            onChange={(p) => void runSearch(p)}
          />
        </div>
      ) : null}
    </div>
  );
}
