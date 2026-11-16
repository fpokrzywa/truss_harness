'use client';

import { useQuery } from '@tanstack/react-query';
import { endpoints } from './api/endpoints';
import type { ArtifactFilters, ArtifactKind } from './api/build-types';

const key = (org: string, ...rest: unknown[]) => ['build', org, ...rest] as const;

/** Slice of SETH's build hooks: only `useArtifacts` (B-2210); the build area extends this file. */
export const useArtifacts = (org: string, kind: ArtifactKind, filters: ArtifactFilters = {}) =>
  useQuery({
    queryKey: key(org, kind, 'list', filters),
    queryFn: () => endpoints.listArtifacts(org, kind, filters),
  });
