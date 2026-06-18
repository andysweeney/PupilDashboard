// supabase/functions/rebuild/index.ts
//
// The browser calls this after an admin finishes uploading raw files. It fires the
// GitHub Actions rebuild — but the school it rebuilds is taken from the caller's
// VERIFIED token (app_metadata.school_id), never from the request body, and only if
// the caller is an admin of that school. So a user can only ever rebuild their own
// school, and only if they're an admin of it. The subdomain is irrelevant here.
//
// Required function secrets: SUPABASE_URL, SUPABASE_ANON_KEY, GH_REPO ("owner/repo"),
// GH_DISPATCH_TOKEN (a fine-grained PAT with "contents: write" / actions dispatch on that repo).

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const json = (b: unknown, status = 200) =>
  new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_ANON_KEY")!,
    { global: { headers: { Authorization: authHeader } } },
  );

  // 1) who is calling? (verified — this validates the JWT signature server-side)
  const { data: { user }, error: uErr } = await sb.auth.getUser();
  if (uErr || !user) return json({ error: "unauthorized" }, 401);

  // 2) which school? from the token's app_metadata (NOT user-writable, NOT from the body)
  const schoolId = (user.app_metadata as Record<string, unknown> | null)?.school_id;
  if (typeof schoolId !== "string" || !/^[a-z0-9][a-z0-9-]{1,62}$/.test(schoolId)) {
    return json({ error: "no valid school on this account" }, 403);
  }

  // 3) admin of THAT school? (admins is tenant-scoped; RLS also enforces this read)
  const { data: adminRow } = await sb
    .from("admins").select("user_id").eq("user_id", user.id).eq("school_id", schoolId).maybeSingle();
  if (!adminRow) return json({ error: "admins only" }, 403);

  // 4) optional academic-year override (validated); otherwise the workflow derives it
  const body = await req.json().catch(() => ({}));
  const ayRaw = (body as Record<string, unknown>)?.academic_year;
  const academic_year = typeof ayRaw === "string" && /^\d{4}$/.test(ayRaw) ? ayRaw : undefined;

  // 5) fire the rebuild
  const gh = await fetch(
    `https://api.github.com/repos/${Deno.env.get("GH_REPO")}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${Deno.env.get("GH_DISPATCH_TOKEN")}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        event_type: "rebuild-dashboard",
        client_payload: { school_id: schoolId, ...(academic_year ? { academic_year } : {}) },
      }),
    },
  );
  if (!gh.ok) return json({ error: "dispatch failed", status: gh.status }, 502);

  return json({ ok: true, school_id: schoolId });
});
