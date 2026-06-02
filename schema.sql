-- SponsorTrack — Supabase schema
-- Run this in Supabase → SQL Editor → New query → Run.
-- NOTE vs the original brief: two columns added so the app works end-to-end:
--   * external_id  (Adzuna job id, UNIQUE) -> lets the scraper UPSERT without duplicating rows each run
--   * category     ('IT' | 'care')         -> powers /jobs?category=... and the frontend filter

CREATE TABLE jobs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  external_id TEXT UNIQUE,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  location TEXT,
  salary TEXT,
  description TEXT,
  source TEXT DEFAULT 'Adzuna',
  apply_url TEXT,
  sponsor_match BOOLEAN DEFAULT FALSE,
  badge TEXT CHECK (badge IN ('verified', 'likely', 'hidden')),
  category TEXT,
  positive_keywords TEXT[],
  negative_keywords TEXT[],
  posted_date DATE,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE email_alerts (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON jobs(badge);
CREATE INDEX ON jobs(posted_date);
CREATE INDEX ON jobs(company);
CREATE INDEX ON jobs(category);
