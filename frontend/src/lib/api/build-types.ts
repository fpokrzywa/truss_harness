// Slice of SETH's build-types.ts: only what governance's scope picker needs (B-2210). The
// build area replaces this file with the full types.

export type ArtifactKind =
  'prompts' | 'agents' | 'tools' | 'skills' | 'orchestrations' | 'pipelines' | 'ontologies';
export type ArtifactVisibility = 'private' | 'scope' | 'tenant';
export type ArtifactStatus = 'active' | 'disabled' | 'retired';

export interface ArtifactScope {
  type: 'tenant' | 'workspace' | 'project';
  id: string;
}

export interface Artifact {
  id: string;
  key: string;
  name: string;
  description?: string | null;
  scope: ArtifactScope;
  visibility: ArtifactVisibility;
  status: ArtifactStatus;
  has_draft: boolean;
  row_version: number;
}

export interface ArtifactFilters {
  scope_type?: string;
  scope_id?: string;
  status?: string;
  q?: string;
}
