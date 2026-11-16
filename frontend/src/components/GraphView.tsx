import { useEffect, useMemo, useRef, useState } from "react";
import type { KgNode, KgPreview, KgRel } from "../types";
import { EDGE_COLORS, EDGE_LABELS, NODE_COLORS, NODE_LABELS, SEMANTIC_EDGES, edgeColor, normalizeEdgeType } from "../lib/graphStyle";

interface PNode { id: string; x: number; y: number; vx: number; vy: number; node: KgNode; deg: number }

/** 轻量力导向图（自实现，百级节点足够）。支持缩放 + 拖拽平移 + 节点拖拽（刷新后复位）。 */
export default function GraphView({ data, onSelect, repulsion = 900 }: { data: KgPreview; onSelect: (n: KgNode | null) => void; repulsion?: number }) {
  const [tick, setTick] = useState(0);
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const simRef = useRef<PNode[]>([]);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [grabbing, setGrabbing] = useState(false);
  // 斥力强度（由父组件控制）；用 ref 让仿真循环读到最新值无需重启
  const repulsionRef = useRef(repulsion);
  const frameRef = useRef(0);
  const stepRef = useRef<(() => void) | null>(null);
  const rafRef = useRef(0);
  useEffect(() => { repulsionRef.current = repulsion; frameRef.current = 0; if (stepRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = requestAnimationFrame(stepRef.current); } }, [repulsion]);
  // 拖拽状态：起点、pan 基准、是否已发生位移（超过阈值算拖拽）
  const dragRef = useRef<{ sx: number; sy: number; bx: number; by: number; moved: boolean } | null>(null);
  // pointerdown 时按在哪个节点上（capture 前解析；capture 后 pointerup 的 target 会变成容器）
  const pressNodeRef = useRef<string | null>(null);
  // 节点拖拽中：记录按住的节点与按下时的偏移（svg 坐标系）
  const dragNodeRef = useRef<{ id: string; dx: number; dy: number } | null>(null);
  const W = 900, H = 560;

  // 屏幕坐标 → SVG 用户坐标（含 viewBox 缩放 / CSS transform / 滚动）
  const toSvg = (clientX: number, clientY: number) => {
    const svg = svgRef.current;
    const ctm = svg?.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const p = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
    return { x: p.x, y: p.y };
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    // 点击缩放按钮等交互元素时不启动拖拽：容器 setPointerCapture 会把后续
    // pointerup/click 重定向到容器，导致按钮 onClick 收不到（缩放按钮失效的根因）
    if ((e.target as HTMLElement).closest("button")) return;
    // 图例区域不参与画布交互：点到图例不应取消选中，也不应启动拖拽。
    // 图例底板在最底层、文字色块在节点之上，两层都带 data-legend。
    if ((e.target as Element).closest("[data-legend]")) return;
    // 节点点击也走 pointerup 语义判断（原生 click 同样会被 capture 重定向吞掉）
    const g = (e.target as Element).closest("[data-node-id]");
    pressNodeRef.current = g ? (g as SVGElement).dataset.nodeId ?? null : null;
    if (pressNodeRef.current) {
      // 按在节点上：记录节点拖拽偏移（位移超阈值后变为节点拖拽而非平移）
      const n = simRef.current.find((p) => p.id === pressNodeRef.current);
      if (n) {
        const pt = toSvg(e.clientX, e.clientY);
        dragNodeRef.current = { id: n.id, dx: n.x - pt.x, dy: n.y - pt.y };
      }
    }
    dragRef.current = { sx: e.clientX, sy: e.clientY, bx: pan.x, by: pan.y, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
    if (!d.moved && Math.abs(dx) + Math.abs(dy) > 4) { d.moved = true; setGrabbing(true); }
    if (!d.moved) return;
    const dn = dragNodeRef.current;
    if (dn) {
      // 节点拖拽：钉到指针位置（钳制在画布内），清零速度防释放后惯性
      const n = simRef.current.find((p) => p.id === dn.id);
      if (n) {
        const pt = toSvg(e.clientX, e.clientY);
        n.x = Math.max(24, Math.min(W - 24, pt.x + dn.dx));
        n.y = Math.max(24, Math.min(H - 24, pt.y + dn.dy));
        n.vx = 0; n.vy = 0;
        setTick((t) => t + 1);
      }
    } else {
      // 空白处拖拽：平移视图
      setPan({ x: d.bx + dx, y: d.by + dy });
    }
  };
  const onPointerUp = () => {
    const d = dragRef.current;
    const pressedId = pressNodeRef.current;
    pressNodeRef.current = null;
    dragNodeRef.current = null;
    dragRef.current = null;
    setGrabbing(false);
    if (d && !d.moved) {
      if (pressedId) {
        // 无位移 + 按在节点上 → 视为节点点击（选中并回调详情面板）
        const hit = simRef.current.find((n) => n.id === pressedId);
        if (hit) { setSelected(pressedId); onSelect(hit.node); }
      } else {
        // 无位移 + 未命中节点 → 点空白：清空选中并收起详情面板。
        // 拖拽（d.moved）与缩放按钮（onPointerDown 提前返回、dragRef 为空）都不会走到这里。
        setSelected(null);
        onSelect(null);
      }
    }
  };
  const resetView = () => { setScale(1); setPan({ x: 0, y: 0 }); };

  const links = useMemo(() => data.relationships.map((r) => ({ r })), [data.relationships]);

  // 构建文档→子分块映射（通过 child 关系）
  const docChildren = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const r of data.relationships) {
      if (r.type === "child") {
        const arr = map.get(r.source) || [];
        arr.push(r.target);
        map.set(r.source, arr);
      }
    }
    return map;
  }, [data.relationships]);

  useEffect(() => {
    const deg: Record<string, number> = {};
    data.relationships.forEach((r) => { deg[r.source] = (deg[r.source] || 0) + 1; deg[r.target] = (deg[r.target] || 0) + 1; });

    // 分离文档节点与分块节点
    const docNodes = data.nodes.filter((n) => n.type === "document");
    const chunkNodes = data.nodes.filter((n) => n.type !== "document");

    // 文档节点：均匀分布在画布中心区域（小半径圆环）
    const docCount = Math.max(docNodes.length, 1);
    const docRadius = docCount > 1 ? 80 : 0; // 多文档时分散，单文档居中

    const positions: PNode[] = [];

    // 1) 放置文档节点（中心区域）
    docNodes.forEach((n, i) => {
      const angle = (i / docCount) * Math.PI * 2;
      positions.push({
        id: n.id,
        x: W / 2 + docRadius * Math.cos(angle),
        y: H / 2 + docRadius * Math.sin(angle),
        vx: 0, vy: 0, node: n, deg: deg[n.id] || 0,
      });
    });

    // 2) 放置分块节点（按所属文档分组，环绕在父文档周围）
    const posMap = new Map(positions.map((p) => [p.id, p]));
    chunkNodes.forEach((n, i) => {
      // 找到父文档（通过 child 关系的 source）
      let parentId: string | null = null;
      for (const [docId, children] of docChildren.entries()) {
        if (children.includes(n.id)) { parentId = docId; break; }
      }
      const parent = parentId ? posMap.get(parentId) : null;

      if (parent) {
        // 环绕父文档：同文档的分块均分一个圆环
        const siblings = docChildren.get(parentId!) || [];
        const siblingIdx = siblings.indexOf(n.id);
        const sibCount = Math.max(siblings.length, 1);
        const ringRadius = 120 + (docCount > 1 ? 40 : 0); // 多文档时环更大避免重叠
        const baseAngle = (siblingIdx / sibCount) * Math.PI * 2;
        // 给每个文档的环加偏移，避免多文档时所有环对齐
        const docIdx = docNodes.findIndex((d) => d.id === parentId!);
        const offsetAngle = docIdx * 0.5;
        positions.push({
          id: n.id,
          x: parent.x + ringRadius * Math.cos(baseAngle + offsetAngle),
          y: parent.y + ringRadius * Math.sin(baseAngle + offsetAngle),
          vx: 0, vy: 0, node: n, deg: deg[n.id] || 0,
        });
      } else {
        // 孤儿分块（无 child 关系）：放到外围
        const angle = (i / Math.max(chunkNodes.length, 1)) * Math.PI * 2;
        const R = 220;
        positions.push({
          id: n.id,
          x: W / 2 + R * Math.cos(angle),
          y: H / 2 + R * Math.sin(angle),
          vx: 0, vy: 0, node: n, deg: deg[n.id] || 0,
        });
      }
    });

    simRef.current = positions;
    // 立即渲染一次初始布局：rAF 在后台标签页会被暂停，不能只靠动画帧驱动重渲染
    setTick((t) => t + 1);
    let frame = 0;
    const step = () => {
      frame = frameRef.current;
      const nodes = simRef.current;
      const idMap = new Map(nodes.map((n) => [n.id, n]));
      // 斥力：只比较 200px 网格里相邻的 9 格（超出 200px 的斥力本就被下面 d2<40000 直接跳过，
      // 但原来要先把每对都算一遍；节点数大时这部分是逐帧的主要 CPU 开销）
      const CELL = 200, CUT2 = 40000;
      const grid = new Map<string, PNode[]>();
      for (const n of nodes) {
        const k = `${Math.floor(n.x / CELL)}:${Math.floor(n.y / CELL)}`;
        const bucket = grid.get(k);
        if (bucket) bucket.push(n); else grid.set(k, [n]);
      }
      for (const a of nodes) {
        const cx = Math.floor(a.x / CELL), cy = Math.floor(a.y / CELL);
        for (let gx = cx - 1; gx <= cx + 1; gx++) {
          for (let gy = cy - 1; gy <= cy + 1; gy++) {
            const bucket = grid.get(`${gx}:${gy}`);
            if (!bucket) continue;
            for (const b of bucket) {
              if (b.id <= a.id) continue; // 每对只施加一次力，力度与原 O(n²) 实现一致
              let dx = a.x - b.x, dy = a.y - b.y;
              const d2 = dx * dx + dy * dy || 1;
              if (d2 < CUT2) {
                const f = repulsionRef.current / d2;
                const d = Math.sqrt(d2);
                dx /= d; dy /= d;
                a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
              }
            }
          }
        }
      }
      // 引力
      for (const { r } of links) {
        const a = idMap.get(r.source), b = idMap.get(r.target);
        if (!a || !b) continue;
        const dx = b.x - a.x, dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = (d - 90) * 0.01;
        a.vx += (dx / d) * f; a.vy += (dy / d) * f;
        b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
      }
      // 向心 + 阻尼（拖拽中的节点钉在指针处，不参与积分；速度清零防释放后惯性）
      for (const n of nodes) {
        if (dragNodeRef.current?.id === n.id) { n.vx = 0; n.vy = 0; continue; }
        n.vx += (W / 2 - n.x) * 0.0015; n.vy += (H / 2 - n.y) * 0.0015;
        if (frame > 40) { n.vx *= 0.6; n.vy *= 0.6; }
        n.x += Math.max(-6, Math.min(6, n.vx)); n.y += Math.max(-6, Math.min(6, n.vy));
        n.x = Math.max(24, Math.min(W - 24, n.x)); n.y = Math.max(24, Math.min(H - 24, n.y));
      }
      frame++;
      frameRef.current = frame;
      // 模拟在 ref 里跑，每 3 帧才提交一次重渲染（拖拽时逐帧），避免 160 帧全量重渲染所有边
      if (frame % 3 === 0 || frame >= 160 || dragNodeRef.current) setTick((t) => t + 1);
      // 帧句柄统一放 rafRef：斥力滑块重启动画时才能取消旧循环，不会两个循环同时跑
      if (frame < 160) rafRef.current = requestAnimationFrame(step);
    };
    stepRef.current = step;
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(rafRef.current); stepRef.current = null; };
  }, [data, links, docChildren]);

  const nodes = simRef.current;
  const idMap = new Map(nodes.map((n) => [n.id, n]));

  // 把当前帧的边拼成 path 字符串，按「类型 + 高亮状态」分组（通常 ≤ 12 个元素）
  const groups = new Map<string, string>();
  for (const r of data.relationships as KgRel[]) {
    const a = idMap.get(r.source), b = idMap.get(r.target);
    if (!a || !b) continue;
    const state = selected ? (r.source === selected || r.target === selected ? "hi" : "dim") : "n";
    const k = `${normalizeEdgeType(r.type)}|${state}`;
    const seg = `M${a.x.toFixed(1)} ${a.y.toFixed(1)}L${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
    groups.set(k, (groups.get(k) || "") + seg);
  }
  const edgePaths = Array.from(groups.entries());

  return (
    <div
      className="relative w-full rounded-md bg-background border border-border overflow-hidden select-none touch-none"
      style={{ minHeight: H, cursor: grabbing ? "grabbing" : "grab" }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      {/* 缩放控制按钮 */}
      <div className="absolute top-2 right-2 z-10 flex gap-1">
        <button className="w-7 h-7 rounded bg-muted/90 border border-border text-foreground hover:bg-primary/10 hover:border-primary/40 hover:text-primary-strong flex items-center justify-center text-sm font-bold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => setScale((s) => Math.min(3, s + 0.25))} title="放大">+</button>
        <button className="w-7 h-7 rounded bg-muted/90 border border-border text-foreground hover:bg-primary/10 hover:border-primary/40 hover:text-primary-strong flex items-center justify-center text-sm font-bold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => setScale((s) => Math.max(0.25, s - 0.25))} title="缩小">−</button>
        <button className="w-7 h-7 rounded bg-muted/90 border border-border text-muted-foreground hover:bg-primary/10 hover:border-primary/40 hover:text-primary-strong flex items-center justify-center text-[11px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={resetView} title="重置缩放与位置">⟲</button>
      </div>
      {/* 调试角标：从 SVG 文本改成容器左上角的 HTML 小徽章 —— 原位置贴右上角，既压在边线上又与缩放按钮重叠。
          仅开发构建显示：import.meta.env.DEV 是 Vite 的构建期常量，生产产物里这段会被摇掉，不暴露给用户。 */}
      {import.meta.env.DEV && (
        <div className="absolute top-2 left-2 z-10 pointer-events-none rounded border border-border bg-card/90 px-1.5 py-0.5 text-[11px] leading-4 text-muted-foreground tabular-nums">
          tick {tick} · {Math.round(scale * 100)}%
        </div>
      )}
      {/* 按 viewBox 自适应宽度：原先 minWidth 900 撑出滚动条、图例被截（视觉整改 V5）；缩放平移仍走 transform */}
      <div className="w-full overflow-hidden">
        <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`}
          style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})`, transformOrigin: "0 0" }}
          className="rounded-md block w-full h-auto">
      {/* 图例底板（最底层）：节点/边绘制在其上，落在图例区域的节点不再被盖住 */}
      <g data-legend transform={`translate(12,${H - 84})`}>
        <rect className="fill-card/90 stroke-border" x={-6} y={-10} width={52} height={38} rx={4} strokeWidth={1} />
      </g>
      <g data-legend transform={`translate(90,${H - 84})`}>
        <rect className="fill-card/90 stroke-border" x={-6} y={-10} width={76} height={74} rx={4} strokeWidth={1} />
      </g>
      {/* 边按 (类型·高亮状态) 合并成少数 path：2000 条边原来是 2000 个 DOM 节点，逐帧重排会卡死 */}
      {edgePaths.map(([k, d]) => {
        const [type, state] = k.split("|");
        return (
          <path key={k} d={d} fill="none"
            style={{ stroke: edgeColor(type) }}
            strokeWidth={state === "hi" ? 1.8 : 0.8}
            opacity={state === "hi" ? 0.95 : state === "dim" ? 0.15 : (SEMANTIC_EDGES.has(type) ? 0.8 : 0.5)} />
        );
      })}
      {nodes.map((n) => {
        const size = n.node.type === "document" ? 12 : 5 + Math.min(6, n.deg);
        const fill = NODE_COLORS[n.node.type] || "#6B6B74";
        const isSel = selected === n.id;
        // 节点标签：document 取文件名尾部；chunk 取首个 theme（退化到正文开头）
        let label: string;
        if (n.node.type === "document") {
          const meta = n.node.properties.document_metadata as Record<string, unknown> | undefined;
          label = String(meta?.filename || "doc").slice(-24);
        } else {
          const themes = Array.isArray(n.node.properties.themes) ? (n.node.properties.themes as unknown[]) : [];
          const raw = themes.length
            ? String(themes[0])
            : String(n.node.properties.page_content || "").replace(/\s+/g, " ").trim();
          label = raw.length > 16 ? raw.slice(0, 16) + "…" : raw;
        }
        return (
          <g key={n.id} data-node-id={n.id} transform={`translate(${n.x},${n.y})`} className="cursor-pointer">
            <circle r={size + 6} fill="transparent" />
            <circle r={size} fill={fill} style={{ stroke: isSel ? "var(--foreground)" : "transparent" }} strokeWidth={2}
              opacity={selected && !isSel ? 0.35 : 1} />
            <text y={n.node.type === "document" ? -size - 8 : size + 14} textAnchor="middle"
              className={n.node.type === "document" ? "fill-foreground" : "fill-muted-foreground"}
              style={{ fontSize: 11, paintOrder: "stroke", stroke: "var(--card)", strokeWidth: 3 }}>
              {label}
            </text>
          </g>
        );
      })}
      {/* 图例文字/色块（节点之上，保证节点压到图例区时文字与色块仍可读）。
          这一层只负责显示、不接收指针（pointer-events: none）：落在图例区的节点照样能点中、能拖；
          点到图例空白处时命中的是最底层带 data-legend 的底板，既不清空选中也不启动拖拽。
          （原先这里盖了一层透明拾取框，把图例区里的节点整块挡住了） */}
      <g transform={`translate(12,${H - 84})`} pointerEvents="none">
        {Object.entries(NODE_COLORS).map(([k, c], i) => (
          <g key={k} transform={`translate(0,${i * 18})`}>
            <circle cx={6} cy={0} r={5} fill={c} /><text x={16} y={4} className="fill-muted-foreground" style={{ fontSize: 11 }}>{NODE_LABELS[k] || k}</text>
          </g>
        ))}
      </g>
      <g transform={`translate(90,${H - 84})`} pointerEvents="none">
        {Object.entries(EDGE_COLORS).map(([k, c], i) => (
          <g key={k} transform={`translate(0,${i * 18})`}>
            <line x1={0} y1={0} x2={14} y2={0} style={{ stroke: c }} strokeWidth={1.5} />
            <text x={20} y={4} className="fill-muted-foreground" style={{ fontSize: 11 }}>{EDGE_LABELS[k] || k}</text>
          </g>
        ))}
      </g>
        </svg>
      </div>
    </div>
  );
}
