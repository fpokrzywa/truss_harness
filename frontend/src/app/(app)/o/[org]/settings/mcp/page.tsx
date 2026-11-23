import type { Metadata } from 'next';
import { McpSettingsView } from '@/components/settings/McpSettingsView';

export const metadata: Metadata = { title: 'MCP access' };

export default function Page() {
  return <McpSettingsView />;
}
