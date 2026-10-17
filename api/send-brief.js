// GET/POST /api/send-brief?secret=CRON_SECRET
// Daily newsletter sender: pulls active subscribers from Supabase, emails the latest brief via Resend.
// Trigger it from any scheduler (cron-job.org, GitHub Actions, Vercel Cron) every day at 23:15 TRT.
// Brief HTML: pass { "html": "...", "subject": "..." } in the body, or it falls back to a plain link.
const SB_URL = process.env.SUPABASE_URL || "https://qpsthoardxlwvqbippoo.supabase.co";
const SB_SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY; // server-side only, never in the browser

export default async function handler(req, res) {
  if (req.query.secret !== process.env.CRON_SECRET) return res.status(401).json({ error: "unauthorized" });
  if (!SB_SERVICE_KEY || !process.env.RESEND_API_KEY) return res.status(500).json({ error: "env missing" });

  const list = await fetch(`${SB_URL}/rest/v1/subscribers?active=eq.true&select=email`, {
    headers: { apikey: SB_SERVICE_KEY, Authorization: `Bearer ${SB_SERVICE_KEY}` },
  }).then(r => r.json());
  if (!Array.isArray(list) || !list.length) return res.status(200).json({ sent: 0 });

  const subject = (req.body && req.body.subject) || "Agyion Quant — Daily Brief";
  const html = (req.body && req.body.html) ||
    `<p>Tonight's signal is live on the site.</p><p><a href="https://agyionlabs.dev">Read the brief</a></p>`;

  let sent = 0;
  // Resend batch: 100 per call max
  for (let i = 0; i < list.length; i += 100) {
    const batch = list.slice(i, i + 100).map(s => ({
      from: process.env.RESEND_FROM, to: s.email, subject, html,
    }));
    const rr = await fetch("https://api.resend.com/emails/batch", {
      method: "POST",
      headers: { Authorization: `Bearer ${process.env.RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify(batch),
    });
    if (rr.ok) sent += batch.length;
  }
  return res.status(200).json({ sent });
}
