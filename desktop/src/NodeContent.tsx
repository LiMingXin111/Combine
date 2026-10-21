import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import {
  FileText,
  Image as ImageIcon,
  Loader2,
  MapPin,
  ShieldCheck,
} from "lucide-react";
import { Markdown } from "./AssistantPanel";
import { rpc } from "./api";
import WebSources from "./WebSources";
type Any = Record<string, any>;

export function AssetImage({
  id,
  alt = "Local image",
}: {
  id: string;
  alt?: string;
}) {
  const [image, setImage] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    if (!id) return;
    rpc("asset.preview", { id })
      .then((v) => {
        if (alive) setImage(v.url);
      })
      .catch(() => {
        if (alive) setError("Image preview unavailable");
      });
    return () => {
      alive = false;
    };
  }, [id]);
  return image ? (
    <img
      className="inline-asset-image"
      src={image}
      alt={alt}
      draggable={false}
    />
  ) : (
    <div className="node-empty-preview">
      <ImageIcon size={22} />
      <span>{error || (id ? "Reading local image…" : "Choose an image")}</span>
    </div>
  );
}

export function MapContent({
  data,
  runId,
  nodeId,
}: {
  data: Any;
  runId?: string;
  nodeId?: string;
}) {
  const value = data.value,
    points: Any[] = Array.isArray(value) ? value : value?.points || [];
  const live = Boolean(value?.live && !data.private && runId && nodeId);
  const element = useRef<HTMLDivElement>(null),
    [active, setActive] = useState(false),
    [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    if (!active || !live || !element.current || !points.length) return;
    const map = L.map(element.current, {
      zoomControl: true,
      attributionControl: true,
      scrollWheelZoom: false,
    });
    let alive = true;
    const Grid = L.GridLayer.extend({
      createTile(coords: L.Coords, done: L.DoneCallback) {
        const tile = document.createElement("img");
        tile.width = tile.height = 256;
        tile.alt = "";
        rpc("map.tile", {
          run_id: runId,
          node_id: nodeId,
          z: coords.z,
          x: coords.x,
          y: coords.y,
        })
          .then((result) => {
            if (!alive) return;
            if (result.available) {
              tile.onload = () => done(undefined, tile);
              tile.src = result.url;
            } else {
              setUnavailable(true);
              done(undefined, tile);
            }
          })
          .catch(() => {
            if (alive) {
              setUnavailable(true);
              done(undefined, tile);
            }
          });
        return tile;
      },
    });
    new (Grid as typeof L.GridLayer)({
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
      noWrap: true,
    }).addTo(map);
    for (const point of points) {
      const label = document.createElement("span");
      label.textContent = point.label;
      L.circleMarker([point.lat, point.lng], {
        color: "#7556d9",
        fillColor: "#8b6bea",
        fillOpacity: 0.9,
        radius: 7,
        weight: 2,
      })
        .bindTooltip(label)
        .addTo(map);
    }
    if (points.length === 1) map.setView([points[0].lat, points[0].lng], 12);
    else
      map.fitBounds(L.latLngBounds(points.map((p) => [p.lat, p.lng])), {
        padding: [35, 35],
        maxZoom: 14,
      });
    const timer = setTimeout(() => map.invalidateSize(), 80);
    const observer = new ResizeObserver(() =>
      map.invalidateSize({ pan: false }),
    );
    observer.observe(element.current);
    return () => {
      alive = false;
      clearTimeout(timer);
      observer.disconnect();
      map.remove();
    };
  }, [active, live, runId, nodeId, JSON.stringify(points)]);
  if (!points.length)
    return (
      <div className="node-empty-preview">
        <MapPin size={22} />
        <span>Connect a table with latitude and longitude.</span>
      </div>
    );
  const minX = Math.min(...points.map((p) => p.lng)),
    maxX = Math.max(...points.map((p) => p.lng)),
    minY = Math.min(...points.map((p) => p.lat)),
    maxY = Math.max(...points.map((p) => p.lat));
  const xy = (p: Any) => [
    30 + ((p.lng - minX) / (maxX - minX || 1)) * 330,
    155 - ((p.lat - minY) / (maxY - minY || 1)) * 125,
  ];
  return (
    <div
      className="map-content nodrag nowheel"
      onWheel={(e) => e.stopPropagation()}
    >
      {active && live ? (
        <div className="live-map-view" ref={element} />
      ) : (
        <svg
          viewBox="0 0 400 190"
          role="img"
          aria-label="Coordinate map"
          className="coordinate-preview"
        >
          <defs>
            <pattern
              id={"map-grid-" + nodeId}
              width="25"
              height="25"
              patternUnits="userSpaceOnUse"
            >
              <path
                d="M 25 0 L 0 0 0 25"
                fill="none"
                stroke="currentColor"
                strokeWidth=".5"
                opacity=".2"
              />
            </pattern>
          </defs>
          <rect width="400" height="190" fill={`url(#map-grid-${nodeId})`} />
          <polyline
            points={points.map((p) => xy(p).join(",")).join(" ")}
            fill="none"
            stroke="#a690e6"
            strokeWidth="2"
          />
          {points.map((p, i) => {
            const [x, y] = xy(p);
            return (
              <g key={i}>
                <circle cx={x} cy={y} r="6" fill="#825fe0" />
                <text x={x + 10} y={y - 9}>
                  {p.label}
                </text>
              </g>
            );
          })}
        </svg>
      )}
      <div className="map-caption">
        <span>
          <MapPin size={11} />
          {points.length} locations
        </span>
        {live && !active ? (
          <button onClick={() => setActive(true)}>Open interactive map</button>
        ) : (
          <small>
            {data.private
              ? "Private coordinates · local view"
              : active
                ? "© OpenStreetMap contributors"
                : "Local coordinate view"}
          </small>
        )}
      </div>
      {unavailable && (
        <small className="warning-text">
          Map tiles are unavailable. Location data is still preserved.
        </small>
      )}
    </div>
  );
}

export default function NodeContent({
  item,
  outputs,
  asset,
  runId,
  nodeId,
  state,
  message,
}: {
  item: Any;
  outputs?: Any;
  asset?: Any;
  runId?: string;
  nodeId?: string;
  state?: string;
  message?: string;
}) {
  if (
    state === "failed" ||
    state === "interrupted" ||
    state === "skipped" ||
    state?.startsWith("waiting")
  )
    return (
      <div
        className={`node-execution-message ${state === "failed" || state === "interrupted" ? "error" : ""}`}
      >
        <strong>
          {state === "interrupted"
            ? "Execution was interrupted"
            : state === "failed"
              ? "Node could not finish"
              : state === "skipped"
                ? "Blocked by an upstream node"
                : "Waiting to continue"}
        </strong>
        <span>{message || "Open the inspector to review this node."}</span>
      </div>
    );
  const values = Object.values(outputs || {}) as Any[];
  const image = values.find((v) => v.kind === "image"),
    map = values.find((v) => v.kind === "map"),
    chart = values.find((v) => v.kind === "chart"),
    table = values.find((v) => v.kind === "table"),
    text = values.find((v) => v.kind === "text");
  if (item.type_id === "data.merge_text" && text)
    return (
      <div className="merge-preview nodrag nowheel">
        <strong>
          {text.segments?.length || item.config.input_count} branches combined
        </strong>
        {text.segments?.map((segment: Any) => (
          <div key={segment.port}>
            <b>{segment.label}</b>
            <small>
              {segment.characters.toLocaleString("en")} characters ·{" "}
              {segment.source_count} sources
            </small>
          </div>
        ))}
        <small>
          Combined text is passed to the next node.{" "}
          {text.private
            ? "Private source restrictions preserved."
            : "Source references preserved."}
        </small>
        <WebSources sources={text.sources} />
      </div>
    );
  if (item.type_id === "network.public_search" && text)
    return (
      <div className="research-preview nodrag nowheel">
        <strong>{table?.value?.rows?.length || 0} public pages found</strong>
        <small>
          Text and source references are available to the next analysis node.
        </small>
        <WebSources sources={text.sources} />
        <details>
          <summary>Preview captured evidence</summary>
          <Markdown text={String(text.value)} />
        </details>
      </div>
    );
  if (image)
    return <AssetImage id={image.value.asset_id} alt={image.value.alt} />;
  if (map) return <MapContent data={map} runId={runId} nodeId={nodeId} />;
  if (
    item.type_id === "input.image" ||
    (item.type_id === "input.file" &&
      /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(asset?.name || ""))
  )
    return <AssetImage id={item.config.asset_id} alt={asset?.name} />;
  if (chart) {
    const data = chart.value.table,
      li = data.columns.indexOf(chart.value.label_column),
      vi = data.columns.indexOf(chart.value.value_column),
      rows = data.rows,
      max = rows.reduce(
        (maximum: number, row: any[]) =>
          Math.max(maximum, Math.abs(Number(row[vi]) || 0)),
        1,
      );
    return (
      <div className="inline-chart nodrag nowheel">
        {rows.map((row: any[], i: number) => (
          <div key={i}>
            <span>{row[li]}</span>
            <i>
              <b
                style={{
                  width: `${(Math.abs(Number(row[vi]) || 0) / max) * 100}%`,
                }}
              />
            </i>
            <strong>{row[vi]}</strong>
          </div>
        ))}
      </div>
    );
  }
  if (table)
    return (
      <div className="inline-table nodrag nowheel">
        <table>
          <thead>
            <tr>
              {table.value.columns.map((c: string, i: number) => (
                <th key={i}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.value.rows.map((row: any[], i: number) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{String(cell ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <small>{table.value.rows.length} rows</small>
      </div>
    );
  const content =
    text?.value ||
    (["input.text", "input.prompt"].includes(item.type_id)
      ? item.config?.text
      : ["input.table", "input.public_table"].includes(item.type_id)
        ? item.config?.csv
        : item.type_id.startsWith("model.")
          ? item.config?.instruction
          : undefined);
  if (typeof content === "string" && content.trim())
    return (
      <div
        className={`inline-document nodrag nowheel ${text ? "has-output" : "source-content"}`}
        onWheel={(e) => e.stopPropagation()}
      >
        <Markdown text={content} />
        <WebSources sources={text?.sources} />
      </div>
    );
  if (item.type_id === "input.file" && asset)
    return (
      <div className="node-file-preview">
        <FileText size={25} />
        <strong>{asset.name}</strong>
        <span>
          {asset.parsed?.status} · {asset.parsed?.parts?.length || 0} source
          locations
        </span>
        <small>
          <ShieldCheck size={11} />
          Private local source
        </small>
      </div>
    );
  return (
    <div className="node-empty-preview">
      {state === "running" && (
        <Loader2 size={16} className="spin" aria-hidden="true" />
      )}
      <span>
        {state === "running"
          ? "Executing this node. Its output appears when it finishes."
          : state === "queued"
            ? "Waiting for its turn or upstream outputs."
            : state === "paused"
              ? "Paused. Resume the workflow to continue."
              : state === "cancelled"
                ? "Cancelled before a completed output was available."
                : state === "not_run"
                  ? "This node did not execute in this run."
                  : item.type_id.startsWith("output.")
                    ? "Output appears here after execution."
                    : item.type_id === "workflow.composite"
                      ? `${item.config?.definition?.nodes?.length || 0} capabilities inside · double-click to explore`
                      : "Connect and configure this capability."}
      </span>
    </div>
  );
}
