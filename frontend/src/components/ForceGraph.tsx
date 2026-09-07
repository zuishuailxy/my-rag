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

function symbolSize(node: GraphViewNode, degree: number) {
  if (node.kind === "document") return 32;
  if (node.kind === "tag") return 16;
  return 18 + Math.min(degree, 8);
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
  // curveness: 边弯曲程度，避免多条边重叠成一条直线
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
        pixelRatio: 2, // 导出倍率，2 比屏幕更清晰
        backgroundColor: "#fafafa",
      });
      const a = document.createElement("a");
      a.href = url;
      a.download = "knowledge-graph.png";
      a.click();
    },
  };
}

function buildOption(
  nodes: GraphViewNode[],
  edges: GraphViewEdge[],
): EChartsOption {
  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }

  return {
    backgroundColor: "transparent",
    tooltip: {
      trigger: "item", // 悬停节点/边时出提示，不跟坐标轴
      confine: true, // 提示框限制在图表内，避免被裁切
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
        layout: "force", // 力导向：节点互相排斥、边拉近，自动铺开
        roam: true, // 允许滚轮缩放、拖动画布
        roamTrigger: "global", // 空白处也能拖拽平移
        draggable: true, // 节点可拖拽
        zoom: 1, // 初始缩放
        scaleLimit: { min: 0.25, max: 4 }, // 缩放上下限
        left: 40,
        right: 40,
        top: 24,
        bottom: 48, // 四周留白，给图例/缩放按钮腾位置
        categories: [...CATEGORIES], // 图例分类，决定节点颜色
        data: nodes.map((node) => ({
          id: node.id,
          name: node.name,
          category: categoryIndex(node),
          symbolSize: symbolSize(node, degree.get(node.id) ?? 1), // 节点圆点大小
          label: {
            show: true,
            position: "bottom" as const,
            distance: 8, // 文字离节点的间距
            color: "#434343",
            fontSize: node.kind === "document" ? 12 : 11,
            fontWeight: node.kind === "document" ? 600 : 400,
            formatter: () => shortName(node.name),
          },
        })),
        links: edges.map((edge) => ({
          source: edge.source,
          target: edge.target,
          relation: edge.relation,
          silent: true, // 边不响应点击/悬停高亮，避免挡节点
          lineStyle: edgeLineStyle(edge.kind),
        })),
        force: {
          repulsion: 160, // 节点互斥力，越大越散
          gravity: 0.1, // 向中心聚拢，防止飞出画布
          edgeLength: 70, // 理想边长
          friction: 0.5, // 阻尼，越大停得越快
          layoutAnimation: false, // 关掉入场动画，大数据更稳
        },
        labelLayout: { hideOverlap: true, moveOverlap: "shiftY" }, // 重叠标签隐藏或纵向错开
        lineStyle: { opacity: 0.9 },
        emphasis: {
          focus: "adjacency", // 高亮当前节点及其相邻边/点
          scale: 1.12, // 悬停时节点略放大
          lineStyle: { width: 2.6 },
          label: { fontWeight: 700 },
        },
        blur: {
          // focus=adjacency 时，非相邻元素走这里，压暗背景
          itemStyle: { opacity: 0.2 },
          lineStyle: { opacity: 0.08 },
          label: { opacity: 0.15 },
        },
        edgeSymbol: ["none", "arrow"], // 起点无标记，终点画箭头
        edgeSymbolSize: [0, 8],
        edgeLabel: { show: false }, // 边上不写字，关系名放 tooltip
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
  const clickRef = useRef(onNodeClick);
  const nodesRef = useRef(nodes);
  clickRef.current = onNodeClick;
  nodesRef.current = nodes;

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;

    let chart: ECharts | null = null;
    let disposed = false;
    let lastSize = { w: 0, h: 0 };

    const render = () => {
      if (disposed || el.clientWidth < 80 || el.clientHeight < 80) return;
      const sizeChanged =
        el.clientWidth !== lastSize.w || el.clientHeight !== lastSize.h;
      lastSize = { w: el.clientWidth, h: el.clientHeight };
      if (!chart) {
        chart = echarts.init(el);
        chart.on("click", (params) => {
          if (params.dataType !== "node") return;
          const id = String((params.data as { id?: string }).id ?? "");
          const node = nodesRef.current.find((n) => n.id === id);
          if (node) clickRef.current?.(node);
        });
        bindHandle(chartRef, chart);
        chart.setOption(buildOption(nodes, edges), { notMerge: true });
        return;
      }
      if (sizeChanged) chart.resize();
    };

    render();
    const ro = new ResizeObserver(render);
    ro.observe(el);

    return () => {
      disposed = true;
      ro.disconnect();
      chart?.dispose();
      if (chartRef) chartRef.current = null;
    };
  }, [chartRef, nodes, edges]);

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
          radius: ["42%", "68%"], // 内外半径，做成环形图
          center: ["50%", "50%"],
          avoidLabelOverlap: true, // 标签自动错开
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
