// Single-series time chart (dataviz spec): 2px line, recessive grid/axes, optional dashed
// threshold, crosshair + tooltip on hover, and a visually-hidden table for screen readers.
import { useEffect, useId, useMemo, useRef, useState } from "react";

interface Props {
  title: string;
  points: [number, number][];
  format: (v: number) => string;
  threshold?: { value: number; label: string };
  height?: number;
  yMin?: number;
  /** Upper bound for the y axis (e.g. 1 for ratios). */
  yCap?: number;
}

const PAD = { top: 8, right: 8, bottom: 22, left: 44 };

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

export function LineChart({ title, points, format, threshold, height = 150, yMin = 0, yCap }: Props) {
  const ref = useRef<SVGSVGElement>(null);
  const box = useRef<HTMLDivElement>(null);
  // Render in real pixels (viewBox = measured width) so text stays legible at any size.
  const [W, setW] = useState(600);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => { if (e) setW(Math.max(240, Math.round(e.contentRect.width))); });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const [hover, setHover] = useState<number | null>(null);
  const tableId = useId();
  const H = height;
  const geo = useMemo(() => {
    if (points.length < 2) return null;
    const t0 = points[0]![0];
    const t1 = points[points.length - 1]![0];
    const maxV = Math.max(...points.map((p) => p[1]), threshold ? threshold.value * 1.15 : 0);
    const yMax = Math.min(niceMax(maxV * 1.1), yCap ?? Number.POSITIVE_INFINITY);
    const x = (t: number) => PAD.left + ((t - t0) / (t1 - t0 || 1)) * (W - PAD.left - PAD.right);
    const y = (v: number) => PAD.top + (1 - (v - yMin) / (yMax - yMin || 1)) * (H - PAD.top - PAD.bottom);
    const d = points.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
    return { t0, t1, yMax, x, y, d };
  }, [points, threshold, H, yMin, yCap, W]);

  const latest = points.length ? points[points.length - 1]![1] : null;
  const head = (
    <div className="chart-title">
      <h3 style={{ margin: 0 }}>{title}</h3>
      <span className="now">{latest == null ? "—" : format(latest)}</span>
    </div>
  );
  if (!geo) {
    return <div className="card" ref={box}>{head}<div className="empty small">No data in this window</div></div>;
  }
  const { t0, t1, yMax, x, y, d } = geo;
  const yTicks = [0, yMax / 2, yMax];
  const xTicks = [0, 1, 2, 3].map((i) => t0 + ((t1 - t0) * i) / 3);
  const hp = hover != null ? points[hover] : undefined;

  const onMove = (e: React.MouseEvent<SVGRectElement>) => {
    const svg = ref.current;
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - PAD.left) / (W - PAD.left - PAD.right)) * (t1 - t0);
    let best = 0;
    for (let i = 1; i < points.length; i++) {
      if (Math.abs(points[i]![0] - t) < Math.abs(points[best]![0] - t)) best = i;
    }
    setHover(best);
  };
  const time = (t: number) =>
    new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  return (
    <div className="card chart" ref={box}>
      {head}
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} role="img" aria-describedby={tableId}
        aria-label={`${title}: latest ${latest == null ? "n/a" : format(latest)}`}>
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)} stroke="var(--grid)" strokeWidth={1} />
            <text x={PAD.left - 6} y={y(v) + 4} textAnchor="end" fontSize={11.5} fill="var(--text-muted)">
              {format(v)}
            </text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={t} x={x(t)} y={H - 6} textAnchor="middle" fontSize={11.5} fill="var(--text-muted)">
            {time(t)}
          </text>
        ))}
        {threshold && threshold.value <= yMax && (
          <g>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(threshold.value)} y2={y(threshold.value)}
              stroke="var(--threshold)" strokeDasharray="4 4" strokeWidth={1} />
            <text x={W - PAD.right} y={y(threshold.value) - 4} textAnchor="end" fontSize={11.5}
              fill="var(--text-secondary)">{threshold.label}</text>
          </g>
        )}
        <path d={d} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round"
          strokeLinecap="round" />
        {hp && (
          <g pointerEvents="none">
            <line x1={x(hp[0])} x2={x(hp[0])} y1={PAD.top} y2={H - PAD.bottom}
              stroke="var(--text-muted)" strokeWidth={1} />
            <circle cx={x(hp[0])} cy={y(hp[1])} r={4} fill="var(--series-1)"
              stroke="var(--surface-1)" strokeWidth={2} />
          </g>
        )}
        <rect x={PAD.left} y={0} width={W - PAD.left - PAD.right} height={H} fill="transparent"
          onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
      </svg>
      {hp && (
        <div className="tip" style={{ left: `${(x(hp[0]) / W) * 100}%`, top: 36 }}>
          <span className="muted">{time(hp[0])}</span> <strong>{format(hp[1])}</strong>
        </div>
      )}
      <table id={tableId} style={{ position: "absolute", width: 1, height: 1, overflow: "hidden",
        clip: "rect(0 0 0 0)" }}>
        <caption>{title}</caption>
        <tbody>
          {points.filter((_, i) => i % Math.ceil(points.length / 12) === 0).map(([t, v]) => (
            <tr key={t}><th scope="row">{time(t)}</th><td>{format(v)}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
