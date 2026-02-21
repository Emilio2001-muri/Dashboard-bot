-- ============================================================
-- NEXUS Trading Dashboard — Supabase Schema
-- Run this in Supabase SQL Editor (https://supabase.com/dashboard)
-- ============================================================

-- Key-Value cache table for all dashboard data
CREATE TABLE IF NOT EXISTS dashboard_cache (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable Row Level Security
ALTER TABLE dashboard_cache ENABLE ROW LEVEL SECURITY;

-- Allow public read (anon key can SELECT)
CREATE POLICY "Allow public read" ON dashboard_cache
  FOR SELECT USING (true);

-- Service role key bypasses RLS for writes (used by data_pusher)
-- No explicit INSERT/UPDATE policy needed for service_role

-- Enable Realtime for live updates
ALTER PUBLICATION supabase_realtime ADD TABLE dashboard_cache;

-- Insert initial rows (all data keys)
INSERT INTO dashboard_cache (key, value) VALUES
  ('prices', '{}'),
  ('mt5_pnl', '{}'),
  ('mt5_trades', '[]'),
  ('mt5_history', '[]'),
  ('mt5_account', '{}'),
  ('indicators', '{}'),
  ('news', '[]'),
  ('calendar', '[]'),
  ('whale_alerts', '[]'),
  ('fear_greed', '{}'),
  ('market_overview', '{}'),
  ('accounts', '[]'),
  ('active_account', '{}'),
  ('global_pnl', '{}')
ON CONFLICT (key) DO NOTHING;
