import type { ApiClient } from './client';
import type { BoardOut, BoardQuery } from './board-types';

const enc = encodeURIComponent;

/** The release board (`shifde.board.api`); the client omits empty filters. */
export function createBoardEndpoints(client: ApiClient) {
  return {
    getBoard: (org: string, q: BoardQuery = {}) =>
      client.request<BoardOut>(`/api/v1/orgs/${enc(org)}/board`, {
        query: { workspace: q.workspace, project: q.project, cursor: q.cursor, limit: q.limit },
      }),
  };
}
