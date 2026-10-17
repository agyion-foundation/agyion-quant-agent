// POST /api/subscribe  { email }
// Writes to Supabase subscribers table. Optional: sends welcome email via Resend if RESEND_API_KEY is set.
const SB_URL = process.env.SUPABASE_URL || "https://qpsthoardxlwvqbippoo.supabase.co";
const SB_KEY = process.env.SUPABASE_KEY || "sb_publishable_YboIulLJgcSAjuPRInotmQ_XDZNMgyp";

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  const email = (req.body && req.body.email || "").trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return res.status(400).json({ error: "invalid email" });

  const r = await fetch(`${SB_URL}/rest/v1/subscribers`, {
    method: "POST",
    headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, "Content-Type": "application/json", Prefer: "return=minimal" },
    body: JSON.stringify({ email }),
  });
  if (r.status === 409) return res.status(200).json({ status: "already" });
  if (!r.ok) return res.status(502).json({ error: "db error" });

  if (process.env.RESEND_API_KEY && process.env.RESEND_FROM) {
    try {
      await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: { Authorization: `Bearer ${process.env.RESEND_API_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          from: process.env.RESEND_FROM,
          to: email,
          subject: "You're on the list — Agyion Quant",
          text: "Next brief lands tonight at 23:15 TRT. One needle, three indices, zero jargon.\n\nAgyion Labs",
        }),
      });
    } catch (_) { /* welcome email is best-effort */ }
  }
  return res.status(200).json({ status: "ok" });
}
