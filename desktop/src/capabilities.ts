export const CAPABILITIES: Record<
  string,
  { label: string; description: string }
> = {
  skill: { label: "Skills", description: "Reusable task procedures" },
  agent: { label: "Agents", description: "Visible multi-stage AI workflows" },
  expert: { label: "Experts", description: "Domain analysis and review roles" },
  input: {
    label: "Inputs",
    description: "Files, images, text and data provided to a workflow",
  },
  research: {
    label: "Web research",
    description: "Public search and page reading",
  },
  knowledge: { label: "Knowledge", description: "Local knowledge retrieval" },
  processing: {
    label: "Processing",
    description: "Deterministic calculation and transformations",
  },
  integration: {
    label: "Integration",
    description: "Combine multiple branches into one context",
  },
  reasoning: {
    label: "AI reasoning",
    description: "Analysis, writing and synthesis with AI",
  },
  visualization: {
    label: "Visualization",
    description: "Images, charts and maps",
  },
  delivery: {
    label: "Deliverables",
    description: "Documents, tables and exported files",
  },
  interaction: {
    label: "Human input",
    description: "Decisions and input provided by a person",
  },
  workflow: {
    label: "Workflows",
    description: "Reusable composite capabilities",
  },
};

export function capabilityKey(type?: Record<string, any>) {
  if (type?.capability && CAPABILITIES[type.capability])
    return type.capability as string;
  return (
    (
      {
        Input: "input",
        Online: "research",
        Knowledge: "knowledge",
        Processing: "processing",
        AI: "reasoning",
        Output: "delivery",
        Interaction: "interaction",
        Workflow: "workflow",
      } as Record<string, string>
    )[type?.category] || "workflow"
  );
}

export function capabilityColor(type?: Record<string, any>) {
  return `var(--cap-${capabilityKey(type)})`;
}
