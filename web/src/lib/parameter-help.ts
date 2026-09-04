import { apiJson } from './api';

export type ParameterDefinition = {
  section: string;
  name: string;
  label: string;
  description: string;
  default?: unknown;
  value_type?: string;
  unit?: string | null;
  minimum?: number | null;
  maximum?: number | null;
  choices?: unknown[] | null;
  applicability?: string | null;
  caveat?: string | null;
};

type ParameterDefinitionResponse = {
  items?: ParameterDefinition[];
};

const sectionRequests = new Map<
  string,
  Promise<Map<string, ParameterDefinition>>
>();

async function loadSection(
  section: string
): Promise<Map<string, ParameterDefinition>> {
  const query = new URLSearchParams({ section, limit: '300' });
  const payload = await apiJson<ParameterDefinitionResponse>(
    `/parameter-definitions?${query}`
  );
  return new Map(
    (payload.items ?? []).map((definition) => [definition.name, definition])
  );
}

export async function parameterDefinition(
  section: string,
  name: string
): Promise<ParameterDefinition | null> {
  let request = sectionRequests.get(section);
  if (!request) {
    request = loadSection(section);
    sectionRequests.set(section, request);
  }
  try {
    return (await request).get(name) ?? null;
  } catch {
    sectionRequests.delete(section);
    return null;
  }
}
