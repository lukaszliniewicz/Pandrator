export type ModelTuningMap = Record<string, Record<string, unknown> | null>;

export function modelTuningMap(value: unknown): ModelTuningMap {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(
      ([, item]) =>
        item === null || (typeof item === 'object' && !Array.isArray(item))
    )
  );
}

/** Mirror the settings resolver: null replaces a model map, objects inherit keys. */
export function mergeModelTuningMaps(...layers: unknown[]): ModelTuningMap {
  const merged: ModelTuningMap = {};
  for (const layer of layers) {
    for (const [model, options] of Object.entries(modelTuningMap(layer))) {
      merged[model] =
        options === null ? null : { ...merged[model], ...options };
    }
  }
  return merged;
}

export function updateModelTuning(
  overrides: ModelTuningMap,
  model: string,
  action: 'inherit' | 'defaults' | { key: string; value: unknown },
  initialValues: Record<string, unknown> = {}
): ModelTuningMap {
  const result = { ...overrides };
  if (action === 'inherit') delete result[model];
  else if (action === 'defaults') result[model] = null;
  else {
    result[model] = {
      ...(result[model] ?? initialValues),
      [action.key]: action.value === '' ? null : action.value
    };
  }
  return result;
}
