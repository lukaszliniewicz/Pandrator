import type { AudioTake, GenerationSegment } from './api-models';

export type PlayableTake = AudioTake & {
  artifact_id: string;
};

export type ComparisonDecisionRow = {
  id: string;
  written: string;
  task?: string;
  signals: string[];
  spoken?: string;
  action?: string;
  confidence?: string | number;
  [key: string]: unknown;
};

export type ReadingBlock = {
  key: string;
  kind: string;
  items: GenerationSegment[];
  closed?: boolean;
};
