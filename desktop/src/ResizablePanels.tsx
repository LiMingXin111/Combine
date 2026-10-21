import { useEffect, useRef, useState } from "react";

const KEY = "combine.desktop.layout.v1";
export const PANEL_DEFAULTS = {
  sidebar: 200,
  assistant: 320,
  library: 260,
  inspector: 270,
  runs: 190,
  answer: 340,
  drawer: 365,
};
export type PanelName = keyof typeof PANEL_DEFAULTS;
type Size = { width: number; height: number };
type Layout = {
  panels: typeof PANEL_DEFAULTS;
  nodes: Record<string, Record<string, Size>>;
};
export const clamp = (value: number, min: number, max: number) =>
  Math.max(min, Math.min(value, Math.max(min, max)));

function readLayout(): Layout {
  const layout: Layout = { panels: { ...PANEL_DEFAULTS }, nodes: {} };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    for (const name of Object.keys(PANEL_DEFAULTS) as PanelName[])
      if (Number.isFinite(saved?.panels?.[name]))
        layout.panels[name] = clamp(saved.panels[name], 120, 1600);
    for (const [context, nodes] of Object.entries(saved?.nodes || {})) {
      const sizes: Record<string, Size> = {};
      for (const [id, size] of Object.entries(nodes || {})) {
        const item = size as Size;
        if (Number.isFinite(item?.width) && Number.isFinite(item?.height))
          sizes[id] = {
            width: clamp(item.width, 180, 1800),
            height: clamp(item.height, 140, 1600),
          };
      }
      layout.nodes[context] = sizes;
    }
  } catch {
    // A disabled storage policy or old preference must not prevent startup.
  }
  return layout;
}

export function useWorkspaceLayout() {
  const [layout, setLayout] = useState(readLayout);
  useEffect(() => {
    const save = () => {
      try {
        localStorage.setItem(KEY, JSON.stringify(layout));
      } catch {
        // The current session remains usable if local storage is unavailable.
      }
    };
    const timer = window.setTimeout(save, 150);
    window.addEventListener("pagehide", save);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("pagehide", save);
    };
  }, [layout]);
  return {
    ...layout,
    setPanel: (name: PanelName, value: number) =>
      setLayout((previous) => ({
        ...previous,
        panels: { ...previous.panels, [name]: value },
      })),
    resetPanel: (name: PanelName) =>
      setLayout((previous) => ({
        ...previous,
        panels: { ...previous.panels, [name]: PANEL_DEFAULTS[name] },
      })),
    resetPanels: () =>
      setLayout((previous) => ({ ...previous, panels: { ...PANEL_DEFAULTS } })),
    setNode: (context: string, id: string, size?: Size) =>
      setLayout((previous) => {
        const nodes = { ...previous.nodes[context] };
        if (size) nodes[id] = size;
        else delete nodes[id];
        return { ...previous, nodes: { ...previous.nodes, [context]: nodes } };
      }),
  };
}

export function useElementSize() {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [size, setSize] = useState({ width: innerWidth, height: innerHeight });
  useEffect(() => {
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ width, height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return { ...size, ref: setElement };
}

export default function ResizeDivider({
  label,
  controls,
  axis = "x",
  direction = 1,
  value,
  min,
  max,
  onChange,
  onReset,
}: {
  label: string;
  controls: string;
  axis?: "x" | "y";
  direction?: 1 | -1;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  onReset: () => void;
}) {
  const drag = useRef<{
    pointer: number;
    origin: number;
    value: number;
  } | null>(null);
  const [active, setActive] = useState(false);
  return (
    <div
      role="separator"
      tabIndex={0}
      className={`resize-divider resize-${axis}`}
      data-active={active}
      aria-label={label}
      aria-controls={controls}
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-valuemin={Math.round(min)}
      aria-valuemax={Math.round(Math.max(min, max))}
      aria-valuenow={Math.round(value)}
      aria-valuetext={`${Math.round(value)} pixels`}
      title={`${label} · Drag or use arrow keys · Double-click to reset`}
      onDoubleClick={onReset}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = {
          pointer: event.pointerId,
          origin: axis === "x" ? event.clientX : event.clientY,
          value,
        };
        setActive(true);
      }}
      onPointerMove={(event) => {
        const start = drag.current;
        if (!start || start.pointer !== event.pointerId) return;
        const delta =
          (axis === "x" ? event.clientX : event.clientY) - start.origin;
        onChange(clamp(start.value + delta * direction, min, max));
      }}
      onPointerUp={(event) => {
        drag.current = null;
        setActive(false);
        if (event.currentTarget.hasPointerCapture(event.pointerId))
          event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onPointerCancel={() => {
        if (drag.current) onChange(drag.current.value);
        drag.current = null;
        setActive(false);
      }}
      onLostPointerCapture={() => {
        drag.current = null;
        setActive(false);
      }}
      onKeyDown={(event) => {
        event.stopPropagation();
        const backward = axis === "x" ? "ArrowLeft" : "ArrowUp";
        const forward = axis === "x" ? "ArrowRight" : "ArrowDown";
        if (
          [backward, forward, "Home", "End", "Enter", "Escape"].includes(
            event.key,
          )
        ) {
          event.preventDefault();
          if (event.key === "Enter") onReset();
          else if (event.key === "Escape" && drag.current) {
            onChange(drag.current.value);
            drag.current = null;
            setActive(false);
          } else if (event.key === "Home") onChange(min);
          else if (event.key === "End") onChange(max);
          else if (event.key !== "Escape")
            onChange(
              clamp(
                value +
                  (event.key === forward ? 1 : -1) *
                    direction *
                    (event.shiftKey ? 40 : 10),
                min,
                max,
              ),
            );
        }
      }}
    >
      <span />
    </div>
  );
}
