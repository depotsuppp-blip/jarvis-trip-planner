import { NextRequest, NextResponse } from "next/server";
import { getDraft, saveDraft } from "@/lib/store";

// params is a Promise in this Next.js version, not a plain object - see
// node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/route.md.
type RouteParams = { params: Promise<{ id: string }> };

export async function GET(_request: NextRequest, { params }: RouteParams) {
  const { id } = await params;
  const draft = await getDraft(id);
  return NextResponse.json({ tripId: id, draft: draft ?? { text: "", updatedAt: null } });
}

export async function POST(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;
  const body = await request.json().catch(() => null);

  if (!body || typeof body.text !== "string") {
    return NextResponse.json(
      { error: "Draft text is required." },
      { status: 400 }
    );
  }

  const draft = await saveDraft(id, body.text);
  return NextResponse.json({ tripId: id, draft }, { status: 201 });
}
