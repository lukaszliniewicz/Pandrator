export const voiceCategories = [
  'male',
  'female',
  'androgynous',
  'unspecified'
] as const;
export type VoiceCategory = (typeof voiceCategories)[number];
export type Character = {
  id: string;
  display_name: string;
  aliases: string[];
  voice_category: VoiceCategory;
  notes: string;
  locked: boolean;
  status: 'proposed' | 'accepted';
  origin: string;
};
export type VoiceBinding = {
  voice?: string;
  voice_id?: string | null;
  service?: string | null;
  model?: string | null;
  voice_description?: string;
};
export type Cast = {
  narrator: VoiceBinding | null;
  categories: Record<string, VoiceBinding>;
  characters: Record<string, VoiceBinding>;
  source_speakers: Record<string, VoiceBinding>;
};
export type GenerationControls = {
  revision: number;
  characters: Character[];
  cast: Cast;
};

export type CastDraftController = {
  draftState: () => { dirty: boolean; blocked: boolean; valid: boolean };
  saveChanges: () => Promise<boolean>;
  discardChanges: () => void;
};
