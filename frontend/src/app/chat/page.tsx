import { api } from "@/lib/api";

import { ChatClient } from "./chat-client";

export const dynamic = "force-dynamic";

export default async function ChatPage() {
  const [leagues, samples] = await Promise.all([api.leagues(), api.chatSamples()]);
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Ask</h1>
      <p className="text-sm text-muted-foreground">
        Answers are checked before they are shown: every number must come from the data, and every claim
        cites its source. That check is why an answer takes a few seconds.
      </p>
      <ChatClient leagues={leagues} samples={samples} />
    </div>
  );
}
