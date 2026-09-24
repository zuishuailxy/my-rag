import { useEffect, useRef, type RefObject } from "react";
import * as echarts from "echarts";
import type { ECharts, EChartsOption } from "echarts";
import type { GraphViewEdge, GraphViewNode } from "../types";

export interface GraphChartHandle {
  zoomIn: () => void;
  zoomOut: () => void;
  reset: () => void;
  exportPng: () => void;
}

interface Props {
  nodes: GraphViewNode[];
  edges: GraphViewEdge[];
  onNodeClick?: (node: GraphViewNode) => void;
  chartRef?: RefObject<GraphChartHandle | null>;
}

const CATEGORIES = [
  { name: "文档", itemStyle: { color: "#1677ff" } },
  { name: "知识点", itemStyle: { color: "#52c41a" } },
  { name: "人物", itemStyle: { color: "#fa8c16" } },
  { name: "组织", itemStyle: { color: "#13c2c2" } },
  { name: "标签", itemStyle: { color: "#722ed1" } },
] as const;

function categoryIndex(node: GraphViewNode) {
  if (node.kind === "document") return 0;
  if (node.kind === "tag") return 4;
  if (node.type === "PERSON") return 2;
  if (node.type === "ORGANIZATION") return 3;
  return 1;
}

function symbolSize(node: GraphViewNode, degree: number, scale = 1) {
  if (node.kind === "document") return Math.round(32 * scale);
  if (node.kind === "tag") return Math.round(16 * scale);
  return Math.round((18 + Math.min(degree, 8)) * scale);
}

function isChatCanvas(height: number) {
  return height > 0 && height < 560;
}

function shortName(name: string) {
  return name.length > 8 ? `${name.slice(0, 8)}…` : name;
}

function kindLabel(node: GraphViewNode) {
  if (node.kind === "document") return "文档";
  if (node.kind === "tag") return "标签";
  if (node.type === "PERSON") return "人物";
  if (node.type === "ORGANIZATION") return "组织";
  return node.type || "知识点";
}

function edgeLineStyle(kind: GraphViewEdge["kind"]) {
  if (kind === "mentions") {
    return {
      color: "#1677ff",
      width: 1.8,
      type: "solid" as const,
      curveness: 0.24,
      opacity: 0.9,
    };
  }
  if (kind === "related") {
    return {
      color: "#8c8c8c",
      width: 1.3,
      type: "dashed" as const,
      curveness: 0.28,
      opacity: 0.85,
    };
  }
  return {
    color: "#722ed1",
    width: 1.3,
    type: "dashed" as const,
    curveness: 0.2,
    opacity: 0.8,
  };
}

function changeZoom(chart: ECharts | null, factor: number) {
  if (!chart) return;
  const option = chart.getOption() as { series?: Array<{ zoom?: number }> };
  const current = Number(option.series?.[0]?.zoom ?? 1);
  const next = Math.min(4, Math.max(0.25, current * factor));
  chart.setOption({ series: [{ zoom: next }] });
}

function bindHandle(chartRef: Props["chartRef"], chart: ECharts) {
  if (!chartRef) return;
  chartRef.current = {
    zoomIn: () => changeZoom(chart, 1.25),
    zoomOut: () => changeZoom(chart, 0.8),
    reset: () => chart.dispatchAction({ type: "restore" }),
    exportPng: () => {
      const url = chart.getDataURL({
        type: "png",
        pixelRatio: 2,
        backgroundColor: "#fafafa",
      });
      const a = document.createElement("a");
      a.href = url;
      a.download = "knowledge-graph.png";
      a.click();
    },
  };
}

function graphDataKey(nodes: GraphViewNode[], edges: GraphViewEdge[]) {
  return [
    nodes.map((node) => node.id).join("\0"),
    edges
      .map((edge) => `${edge.source}\t${edge.target}\t${edge.kind}`)
      .join("\0"),
  ].join("|");
}

function buildOption(
  nodes: GraphViewNode[],
  edges: GraphViewEdge[],
  compact: boolean,
): EChartsOption {
  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }

  return {
    animation: false,
    animationDuration: 0,
    animationDurationUpdate: 0,
    backgroundColor: "transparent",
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (raw) => {
        const params = raw as {
          dataType?: string;
          data?: { id?: string; name?: string; relation?: string };
        };
        if (params.dataType === "edge") {
          return `<div style="padding:4px 2px">${params.data?.relation || "关联"}</div>`;
        }
        const node = nodes.find((n) => n.id === params.data?.id);
        if (!node) return params.data?.name ?? "";
        const desc = node.description
          ? `<div style="color:#8c8c8c;margin-top:4px;max-width:280px;white-space:normal">${node.description}</div>`
          : "";
        return `<div style="padding:4px 2px"><b>${node.name}</b><div style="color:#1677ff;margin-top:4px">${kindLabel(node)}</div>${desc}</div>`;
      },
    },
    series: [
      {
        type: "graph",
        layout: "force",
        roam: true,
        roamTrigger: "global",
        draggable: true,
        zoom: compact ? 1.35 : 1,
        scaleLimit: { min: 0.25, max: 4 },
        left: compact ? 20 : 40,
        right: compact ? 20 : 40,
        top: compact ? 20 : 24,
        bottom: compact ? 32 : 48,
        categories: [...CATEGORIES],
        data: nodes.map((node) => ({
          id: node.id,
          name: node.name,
          category: categoryIndex(node),
          symbolSize: symbolSize(
            node,
            degree.get(node.id) ?? 1,
            compact ? 1.28 : 1,
          ),
          label: {
            show: true,
            position: "bottom" as const,
            distance: 8,
            color: "#434343",
            fontSize:
              node.kind === "document"
                ? compact
                  ? 13
                  : 12
                : compact
                  ? 12
                  : 11,
            fontWeight: node.kind === "document" ? 600 : 400,
            formatter: () => shortName(node.name),
          },
        })),
        links: edges.map((edge) => ({
          source: edge.source,
          target: edge.target,
          relation: edge.relation,
          silent: true,
          lineStyle: edgeLineStyle(edge.kind),
        })),
        // layoutAnimation 必须开，关掉节点会叠在原点只剩箭头空转
        force: compact
          ? {
              repulsion: 240,
              gravity: 0.05,
              edgeLength: 110,
              friction: 0.72,
              layoutAnimation: true,
            }
          : {
              repulsion: 160,
              gravity: 0.12,
              edgeLength: 70,
              friction: 0.68,
              layoutAnimation: true,
            },
        labelLayout: { hideOverlap: true, moveOverlap: "shiftY" },
        lineStyle: { opacity: 0.9 },
        emphasis: {
          focus: "adjacency",
          scale: 1.12,
          lineStyle: { width: 2.6 },
          label: { fontWeight: 700 },
        },
        blur: {
          itemStyle: { opacity: 0.2 },
          lineStyle: { opacity: 0.08 },
          label: { opacity: 0.15 },
        },
        edgeSymbol: ["none", "arrow"],
        edgeSymbolSize: [0, 8],
        edgeLabel: { show: false },
      },
    ],
  };
}

export default function ForceGraph({
  nodes,
  edges,
  onNodeClick,
  chartRef,
}: Props) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartInnerRef = useRef<ECharts | null>(null);
  const clickRef = useRef(onNodeClick);
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  clickRef.current = onNodeClick;
  nodesRef.current = nodes;
  edgesRef.current = edges;
  const dataKey = graphDataKey(nodes, edges);

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;

    let disposed = false;
    let lastSize = { w: 0, h: 0 };

    const applyOption = (chart: ECharts) => {
      chart.setOption(
        buildOption(
          nodesRef.current,
          edgesRef.current,
          isChatCanvas(el.clientHeight),
        ),
        { notMerge: true },
      );
    };

    const mount = () => {
      if (disposed || chartInnerRef.current) return;
      if (el.clientWidth < 80 || el.clientHeight < 80) return;
      const chart = echarts.init(el);
      chartInnerRef.current = chart;
      lastSize = { w: el.clientWidth, h: el.clientHeight };
      chart.on("click", (params) => {
        if (params.dataType !== "node") return;
        const id = String((params.data as { id?: string }).id ?? "");
        const node = nodesRef.current.find((n) => n.id === id);
        if (node) clickRef.current?.(node);
      });
      bindHandle(chartRef, chart);
      applyOption(chart);
    };

    const onResize = () => {
      if (disposed) return;
      if (!chartInnerRef.current) {
        mount();
        return;
      }
      const w = el.clientWidth;
      const h = el.clientHeight;
      if (w === lastSize.w && h === lastSize.h) return;
      lastSize = { w, h };
      chartInnerRef.current.resize();
    };

    mount();
    const ro = new ResizeObserver(onResize);
    ro.observe(el);
    return () => {
      disposed = true;
      ro.disconnect();
      chartInnerRef.current?.dispose();
      chartInnerRef.current = null;
      if (chartRef) chartRef.current = null;
    };
  }, [chartRef]);

  useEffect(() => {
    const chart = chartInnerRef.current;
    if (!chart) return;
    const el = elRef.current;
    chart.setOption(
      buildOption(
        nodesRef.current,
        edgesRef.current,
        isChatCanvas(el?.clientHeight ?? 0),
      ),
      { notMerge: true },
    );
  }, [dataKey]);

  return <div ref={elRef} className="kh-force-wrap" />;
}

interface PieProps {
  items: Array<{ type: string; count: number }>;
}

export function EntityTypePie({ items }: PieProps) {
  const elRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    const chart = echarts.init(el);
    const data = items.length
      ? items.map((item) => ({ name: item.type, value: item.count }))
      : [{ name: "暂无", value: 0 }];
    chart.setOption({
      tooltip: { trigger: "item" },
      series: [
        {
          type: "pie",
          radius: ["42%", "68%"],
          center: ["50%", "50%"],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: "#fff", borderWidth: 2 },
          label: { fontSize: 11, color: "#595959" },
          data,
        },
      ],
    } satisfies EChartsOption);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
    };
  }, [items]);

  return <div ref={elRef} className="kh-type-pie" />;
}
