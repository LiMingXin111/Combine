import { NodeResizeControl, ResizeControlVariant } from "@xyflow/react";
import { Maximize2, RotateCcw } from "lucide-react";
import { useCallback, useRef } from "react";

export default function NodeSizeControls({
  selected,
  note,
  onResize,
  onReset,
}: {
  selected?: boolean;
  note?: boolean;
  onResize: (size: { width: number; height: number }) => void;
  onReset: () => void;
}) {
  const resize = useRef(onResize);
  resize.current = onResize;
  const handleResize = useCallback(
    (_event: unknown, size: { width: number; height: number }) => {
      resize.current({ width: size.width, height: size.height });
    },
    [],
  );
  if (!selected) return null;
  const props = {
    minWidth: note ? 180 : 280,
    minHeight: note ? 140 : 200,
    maxWidth: 1800,
    maxHeight: 1600,
    onResize: handleResize,
  };
  return (
    <>
      <NodeResizeControl
        {...props}
        position="right"
        variant={ResizeControlVariant.Line}
        resizeDirection="horizontal"
        className="node-size-line"
      />
      <NodeResizeControl
        {...props}
        position="bottom"
        variant={ResizeControlVariant.Line}
        resizeDirection="vertical"
        className="node-size-line"
      />
      <NodeResizeControl
        {...props}
        position="bottom-right"
        className="node-size-handle"
      >
        <Maximize2 size={12} aria-label="Drag to resize node" />
      </NodeResizeControl>
      <button
        className="node-size-reset nodrag"
        aria-label="Reset node size"
        title="Reset node size"
        onClick={(event) => {
          event.stopPropagation();
          onReset();
        }}
      >
        <RotateCcw size={12} />
      </button>
    </>
  );
}
