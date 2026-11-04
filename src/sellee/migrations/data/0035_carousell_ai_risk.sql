-- carousell.ai's own label on a buyer message it screened: its risk, and its relay flags as a JSON
-- list. NULL on every other message, and on one carousell.ai never screened.
ALTER TABLE thread_messages ADD COLUMN carousell_ai_risk TEXT;
ALTER TABLE thread_messages ADD COLUMN carousell_ai_flags TEXT;
