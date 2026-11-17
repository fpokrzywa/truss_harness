import type { ErrorClass } from './api/operate-types';

export interface FailureInfo {
  title: string;
  explanation: string;
  retryable: boolean;
  action: string;
}

/** Error class -> human explanation (SYSTEM_ARCHITECTURE section 6.7 classification). */
export const FAILURE_INFO: Record<ErrorClass, FailureInfo> = {
  validation: {
    title: 'Validation failed',
    explanation: 'The input, variables or arguments did not satisfy the declared schema.',
    retryable: false,
    action: 'Correct the input or variables and start a new run.',
  },
  authz_denied: {
    title: 'Authorization denied',
    explanation:
      'The Execution Gate refused an action because the principal lacks a current grant for it.',
    retryable: false,
    action: 'Ask an administrator to grant the missing permission, then start a new run.',
  },
  budget_denied: {
    title: 'Budget exhausted',
    explanation: 'A token or run budget for this scope was reached, so the run was not allowed on.',
    retryable: false,
    action: 'Wait for the budget period to reset or ask an administrator to raise the limit.',
  },
  provider_unavailable: {
    title: 'Model provider unavailable',
    explanation: 'The model provider could not be reached or returned a server error.',
    retryable: true,
    action: 'Retry shortly; configure a fallback model if this keeps happening.',
  },
  rate_limited: {
    title: 'Rate limited',
    explanation: 'The provider or a tool throttled the request.',
    retryable: true,
    action: 'Retry later; the harness already backs off and honors Retry-After.',
  },
  timeout: {
    title: 'Timed out',
    explanation: 'A model call, tool call or the run deadline was exceeded.',
    retryable: true,
    action: 'Retry, or raise the timeout or deadline on the agent or tool.',
  },
  transient_network: {
    title: 'Transient network error',
    explanation: 'A temporary network failure interrupted a call.',
    retryable: true,
    action: 'Retry; check connectivity and egress policy if it persists.',
  },
  provider_error_4xx: {
    title: 'Provider rejected the request',
    explanation:
      'The provider returned a client error (for example a bad model name or credential).',
    retryable: false,
    action: 'Check the model, connection credentials and request size, then start a new run.',
  },
  tool_error: {
    title: 'Tool failed',
    explanation: 'A tool returned an error or could not complete its work.',
    retryable: false,
    action: 'Inspect the failing step, fix the tool or its target system, then start a new run.',
  },
  schema_violation: {
    title: 'Output did not match schema',
    explanation: 'A model or tool produced output that violates its declared schema.',
    retryable: false,
    action: 'Tighten the prompt or schema, or fix the tool output, then start a new run.',
  },
  approval_rejected: {
    title: 'Approval rejected',
    explanation: 'An approver rejected the requested action, so the run was ended.',
    retryable: false,
    action: 'Review the approver comment; start a new run if the request should be resubmitted.',
  },
  cancelled: {
    title: 'Cancelled',
    explanation: 'The run was cancelled before it finished.',
    retryable: false,
    action: 'Start a new run if the work is still needed.',
  },
  indeterminate_side_effect: {
    title: 'Side effect outcome unknown',
    explanation:
      'A tool with a side effect started but its outcome was not recorded, so the harness cannot tell whether it happened.',
    retryable: false,
    action:
      'Verify the effect in the target system, then decide in Approvals: mark succeeded, retry or fail.',
  },
  internal: {
    title: 'Internal error',
    explanation: 'The platform hit an unexpected error.',
    retryable: false,
    action: 'Report the run id and trace id to an administrator.',
  },
};

export function failureInfo(errorClass: string | null | undefined): FailureInfo | null {
  if (!errorClass) return null;
  return (
    FAILURE_INFO[errorClass as ErrorClass] ?? {
      title: errorClass,
      explanation: 'The server reported an error class this UI does not recognize.',
      retryable: false,
      action: 'See the error message and trace id, or contact an administrator.',
    }
  );
}
