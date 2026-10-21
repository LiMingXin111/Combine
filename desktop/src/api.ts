import { invoke, isTauri } from "@tauri-apps/api/core";

let activeSpace: string | undefined;
export function currentSpace() {
  return activeSpace;
}

export async function rpc<T = any>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  const requestId = crypto.randomUUID();
  const pinnedSpace = activeSpace;
  params = { ...params, ...(pinnedSpace ? { _space_id: pinnedSpace } : {}) };
  const started = performance.now();
  const trace = (status: string) => {
    if (import.meta.env.DEV)
      console.debug("[Combine RPC]", {
        requestId,
        method,
        status,
        elapsedMs: Math.round(performance.now() - started),
      });
  };
  trace("started");
  try {
    let response: any;
    if (isTauri())
      response = await invoke("desktop_rpc", { method, params, requestId });
    else {
      const result = await fetch("/desktop-rpc", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Combine-Desktop": "1",
        },
        body: JSON.stringify({ id: requestId, method, params }),
      });
      if (!result.ok)
        throw new Error(
          "The local development bridge is unavailable. Start the desktop runtime.",
        );
      response = await result.json();
    }
    if (response.error) throw new Error(response.error.message);
    if (
      method === "workspace.snapshot" &&
      response.result?.workspace?.space_id
    ) {
      if (activeSpace && response.result.workspace.space_id !== activeSpace)
        throw new Error("Discarded a response from a different space.");
      activeSpace = response.result.workspace.space_id;
    }
    trace("finished");
    return response.result;
  } catch (error) {
    trace("failed");
    throw error;
  }
}

export async function download(filename: string, content: string) {
  if (isTauri()) return invoke("save_export", { filename, content });
  const url = URL.createObjectURL(
    new Blob([content], { type: "text/plain;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function localFiles(files: FileList | File[]) {
  const imported = [];
  for (const file of Array.from(files)) {
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    imported.push(
      await rpc("asset.import", { name: file.name, content_base64: data }),
    );
  }
  return imported;
}
