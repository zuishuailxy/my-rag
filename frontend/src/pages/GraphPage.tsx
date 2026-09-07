import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CompressOutlined,
  DownloadOutlined,
  MinusOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import {
  Button,
  DatePicker,
  Empty,
  Input,
  Select,
  Space,
  Spin,
  message,
} from "antd";
import { graphApi } from "../api";
import { ApiError } from "../api/client";
import ForceGraph, {
  EntityTypePie,
  type GraphChartHandle,
} from "../components/ForceGraph";
import type { GraphOverview, GraphViewNode } from "../types";
import { formatTime } from "../utils";

const emptyOverview: GraphOverview = {
  nodes: [],
  edges: [],
  stats: {
    nodeCount: 0,
    edgeCount: 0,
    documentCount: 0,
    entityCount: 0,
    tagCount: 0,
    mentionCount: 0,
    relatedCount: 0,
    entityTypes: [],
  },
  topEntities: [],
  recentNodes: [],
  entityTypes: [],
};

export default function GraphPage() {
  const navigate = useNavigate();
  const [keyword, setKeyword] = useState("");
  const [entityType, setEntityType] = useState<string>();
  const [from, setFrom] = useState<string>();
  const [to, setTo] = useState<string>();
  const [data, setData] = useState<GraphOverview>(emptyOverview);
  const [loading, setLoading] = useState(false);
  const [pickerKey, setPickerKey] = useState(0);
  const [selected, setSelected] = useState<GraphViewNode | null>(null);
  const chartRef = useRef<GraphChartHandle | null>(null);

  async function load(next?: {
    keyword?: string;
    entityType?: string | null;
    from?: string | null;
    to?: string | null;
  }) {
    setLoading(true);
    try {
      const res = await graphApi.overview({
        keyword: (next?.keyword ?? keyword).trim() || undefined,
        entityType:
          (next && "entityType" in next ? next.entityType : entityType) ||
          undefined,
        from: (next && "from" in next ? next.from : from) || undefined,
        to: (next && "to" in next ? next.to : to) || undefined,
        docLimit: 24,
      });
      setData(res);
      setSelected(null);
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "图谱加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const typeOptions = data.entityTypes.map((t) => ({ value: t, label: t }));

  return (
    <div className="kh-graph-layout">
      <div className="kh-graph-main">
        <div className="kh-graph-toolbar">
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: "#bfbfbf" }} />}
            placeholder="输入关键词检索图谱…"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onPressEnter={() => void load()}
            style={{ width: 240 }}
          />
          <Select
            allowClear
            placeholder="节点类型"
            style={{ width: 150 }}
            value={entityType}
            onChange={(v) => {
              setEntityType(v);
              void load({ entityType: v ?? null });
            }}
            options={typeOptions}
          />
          <DatePicker.RangePicker
            key={pickerKey}
            onChange={(dates) => {
              setFrom(dates?.[0]?.toISOString());
              setTo(dates?.[1]?.toISOString());
            }}
          />
          <Button
            onClick={() => {
              setKeyword("");
              setEntityType(undefined);
              setFrom(undefined);
              setTo(undefined);
              setPickerKey((k) => k + 1);
              void load({
                keyword: "",
                entityType: null,
                from: null,
                to: null,
              });
            }}
          >
            重置
          </Button>
          <Button type="primary" loading={loading} onClick={() => void load()}>
            检索
          </Button>
          <div style={{ flex: 1 }} />
          <Button
            icon={<DownloadOutlined />}
            onClick={() => chartRef.current?.exportPng()}
          >
            导出图谱
          </Button>
        </div>
        <div className="kh-graph-canvas">
          {data.nodes.length ? (
            <ForceGraph
              nodes={data.nodes}
              edges={data.edges}
              chartRef={chartRef}
              onNodeClick={(node) => setSelected(node)}
            />
          ) : (
            <div className="kh-graph-empty">
              {loading ? (
                <Spin />
              ) : (
                <Empty description="暂无图谱数据，发布文档后会写入 Neo4j" />
              )}
            </div>
          )}
          {loading && data.nodes.length ? (
            <div className="kh-graph-loading">
              <Spin />
            </div>
          ) : null}
          <div className="kh-graph-legend">
            <span>
              <i style={{ background: "#1677ff" }} /> 文档
            </span>
            <span>
              <i style={{ background: "#52c41a" }} /> 知识点
            </span>
            <span>
              <i style={{ background: "#fa8c16" }} /> 人物
            </span>
            <span>
              <i style={{ background: "#13c2c2" }} /> 组织
            </span>
            <span>
              <i style={{ background: "#722ed1" }} /> 标签
            </span>
            <span>
              <span className="kh-legend-line kh-legend-blue" /> 提及
            </span>
            <span>
              <span className="kh-legend-dash kh-legend-grey" /> 关联
            </span>
            <span>
              <span className="kh-legend-dash kh-legend-purple" /> 标注
            </span>
          </div>
          <div className="kh-graph-zoom">
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() => chartRef.current?.zoomIn()}
            />
            <Button
              size="small"
              icon={<MinusOutlined />}
              onClick={() => chartRef.current?.zoomOut()}
            />
            <Button
              size="small"
              icon={<CompressOutlined />}
              onClick={() => chartRef.current?.reset()}
            />
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void load()}
            />
          </div>
          <div className="kh-graph-hint">
            可拖拽节点，滚轮缩放；点击文档节点打开正文
          </div>
        </div>
      </div>
      <aside className="kh-graph-side">
        <div className="kh-graph-card">
          <h4>图谱数据统计</h4>
          <div className="kh-graph-stats">
            <div>
              <b>{data.stats.documentCount}</b>
              <span>文档节点</span>
            </div>
            <div>
              <b>{data.stats.entityCount}</b>
              <span>知识点</span>
            </div>
            <div>
              <b>{data.stats.relatedCount}</b>
              <span>实体关系</span>
            </div>
            <div>
              <b>{data.stats.mentionCount}</b>
              <span>文档提及</span>
            </div>
            <div>
              <b>{data.stats.tagCount}</b>
              <span>当前标签</span>
            </div>
            <div>
              <b>{data.stats.edgeCount}</b>
              <span>画布边数</span>
            </div>
          </div>
        </div>
        <div className="kh-graph-card">
          <h4>知识点类型分布</h4>
          {data.stats.entityTypes.length ? (
            <EntityTypePie items={data.stats.entityTypes} />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无" />
          )}
        </div>
        <div className="kh-graph-card">
          <h4>热门知识点 TOP5</h4>
          {data.topEntities.length ? (
            <ol className="kh-graph-rank">
              {data.topEntities.map((e, i) => (
                <li key={e.name}>
                  <span className="kh-rank">{i + 1}</span>
                  <span className="kh-rank-name">{e.name}</span>
                  <span className="kh-rank-n">{e.degree}</span>
                </li>
              ))}
            </ol>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无" />
          )}
        </div>
        <div className="kh-graph-card">
          <h4>最近更新节点</h4>
          {data.recentNodes.map((n) => (
            <div key={n.id} className="kh-graph-recent">
              <div>{n.name}</div>
              <small>{formatTime(n.updatedAt)}</small>
            </div>
          ))}
        </div>
        {selected ? (
          <div className="kh-graph-card">
            <h4>当前节点</h4>
            <Space direction="vertical" size={4}>
              <div>{selected.name}</div>
              <div style={{ color: "#8c8c8c", fontSize: 12 }}>
                {selected.kind === "document"
                  ? "文档"
                  : selected.kind === "tag"
                    ? "标签"
                    : selected.type || "知识点"}
              </div>
              {selected.description ? <div>{selected.description}</div> : null}
              {selected.documentId ? (
                <Button
                  type="link"
                  style={{ padding: 0 }}
                  onClick={() => navigate(`/documents/${selected.documentId}`)}
                >
                  打开文档
                </Button>
              ) : null}
            </Space>
          </div>
        ) : null}
      </aside>
    </div>
  );
}
