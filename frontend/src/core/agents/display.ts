export function displayNameOfAgent(
  agentName: string,
  configuredName?: string | null,
): string {
  if (agentName === "tender-review") return "招投标智能体";
  return configuredName ?? agentName;
}
